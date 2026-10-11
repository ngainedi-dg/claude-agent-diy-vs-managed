"""Check the numbers an agent reports against the raw data, where that can be done without guessing.

An agent names its own metrics ("west_revenue", "orders_monday"), so most evidence cannot be
checked mechanically. This module checks the narrow set it can, conservatively:

  - the metric name must be built only from words we understand (a kind such as revenue,
    count, quantity or mean discount, plus harmless filler and the finding's own segment values);
    anything else (a month, a weekday, "share", "per") is left unverifiable;
  - the number is recomputed on slices of the finding's segment (region, category, product,
    customer, date range): the full segment and every sub-combination of its keys, because a
    finding's segment says what it is about, not always exactly which rows a number covers;
  - because agents clean data differently, it is tried on four cleaning variants (raw,
    de-duplicated, cleaned, cleaned without extreme quantities).

A claim is "supported" if it matches any of those. "Unsupported" means it matched none, which is
NOT a finding that it is wrong: the agent may have used a sub-selection we cannot express (such as
"the 19 rows with discount over 40%"). "Unverifiable" means we could not tell what the metric meant.
Support is a lower bound on correctness, and a loose one: a number can match by coincidence.
"""

import re
from itertools import chain, combinations

import pandas as pd

KIND_WORDS = {
    "revenue": {"revenue", "sales"},
    "count": {"transactions", "transaction", "orders", "order", "rows", "row", "count", "txns", "n"},
    "quantity": {"quantity", "units"},
    "discount": {"discount"},
}
FILLER = {"total", "recorded", "raw", "clean", "cleaned", "net", "gross", "number", "num", "of", "the", "all",
          "dataset", "in", "overall", "mean", "avg", "average", "usd", "pct"}
BLOCKING = {"share", "percent", "percentage", "ratio", "per", "rate", "z", "zscore", "std", "max", "min", "median",
            "change", "growth", "vs", "delta", "diff", "expected", "baseline", "shortfall", "drop", "decline", "gap",
            "lift", "multiple", "excess", "extra", "avg_order", "p95", "unique", "distinct"}
EXTREME_QUANTITY = 50  # ordinary orders in this dataset are far below this; injected outliers are 400+
REL_TOL, ABS_TOL = 0.01, 0.5


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(text).lower()) if t]


def variants(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    dedup = df.drop_duplicates()
    expected = (dedup.quantity * dedup.unit_price * (1 - dedup.discount_pct / 100)).round(2)
    consistent = (dedup.revenue - expected).abs() <= 0.02 + 0.001 * expected.abs()
    cleaned = dedup[(dedup.quantity > 0) & consistent]
    return {"raw": df, "deduplicated": dedup, "cleaned": cleaned,
            "cleaned_no_extremes": cleaned[cleaned.quantity <= EXTREME_QUANTITY]}


def slice_keys(segment: dict) -> list[tuple[str, ...]]:
    """Every sub-combination of the segment's filterable keys, from all of them down to none (the whole dataset)."""
    keys = sorted(k for k in segment if k != "transaction_ids")
    return sorted(set(chain.from_iterable(combinations(keys, n) for n in range(len(keys) + 1))), key=lambda c: -len(c))


def apply_segment(df: pd.DataFrame, segment: dict) -> pd.DataFrame:
    """The rows matching the segment's keys."""
    out = df
    if "region" in segment:
        out = out[out.region.str.strip().str.lower() == str(segment["region"]).strip().lower()]
    if "category" in segment:
        out = out[out.category.astype(str).str.strip().str.lower() == str(segment["category"]).strip().lower()]
    if "product_id" in segment:
        out = out[out.product_id == segment["product_id"]]
    if "customer_id" in segment:
        out = out[out.customer_id == segment["customer_id"]]
    if "date_range" in segment:
        lo, hi = segment["date_range"]
        out = out[(out.date >= lo) & (out.date <= hi)]
    return out


def classify_metric(name: str, segment: dict) -> str | None:
    """Which statistic a metric name is asking for, or None if we cannot tell with confidence."""
    toks = tokens(name)
    if not toks or BLOCKING & set(toks) or "_".join(toks).startswith("avg_order"):
        return None
    segment_words = {t for v in segment.values() if isinstance(v, str) for t in tokens(v)}
    kinds = [k for k, words in KIND_WORDS.items() if words & set(toks)]
    if len(kinds) != 1:
        return None
    kind = kinds[0]
    if kind == "discount" and not ({"mean", "avg", "average"} & set(toks)):
        return None
    known = set().union(*KIND_WORDS.values()) | FILLER | segment_words
    if set(toks) - known:  # an unrecognised qualifier (a month, a weekday...) changes what the number means
        return None
    return kind


def statistic(frame: pd.DataFrame, kind: str) -> float | None:
    if frame.empty:
        return None
    return {"revenue": lambda f: float(f.revenue.sum()), "count": lambda f: float(len(f)),
            "quantity": lambda f: float(f.quantity.sum()), "discount": lambda f: float(f.discount_pct.mean())}[kind](frame)


def close(claimed: float, actual: float, kind: str) -> bool:
    if kind == "discount":
        return abs(claimed - actual) <= 0.1
    return abs(claimed - actual) <= max(REL_TOL * abs(actual), ABS_TOL)


def check_claims(findings: list[dict], df: pd.DataFrame | None) -> dict:
    if df is None:
        return {"available": False}
    versions = variants(df)
    supported, unsupported, unverifiable = 0, [], 0
    for f in findings:
        segment = f.get("segment") or {}
        for item in f.get("evidence", []):
            value, kind = item.get("value"), classify_metric(str(item.get("metric", "")), segment)
            if kind is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                unverifiable += 1
                continue
            if segment.get("transaction_ids") and not [k for k in segment if k != "transaction_ids"]:
                unverifiable += 1  # a sample of IDs does not say which rows the number covers
                continue
            candidates = []
            for keys in slice_keys(segment):
                sub = {k: segment[k] for k in keys}
                for frame in versions.values():
                    actual = statistic(apply_segment(frame, sub), kind)
                    if actual is not None:
                        candidates.append(actual)
            if not candidates:
                unverifiable += 1
            elif any(close(value, a, kind) for a in candidates):
                supported += 1
            else:
                unsupported.append({"finding": f.get("finding_id"), "metric": item.get("metric"), "kind": kind,
                                    "claimed": value})
    total = supported + len(unsupported) + unverifiable
    return {"available": True, "supported": supported, "unsupported": len(unsupported), "unverifiable": unverifiable,
            "checked_fraction": None if not total else round((supported + len(unsupported)) / total, 3),
            "unsupported_details": unsupported}
