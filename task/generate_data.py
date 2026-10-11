"""Generate the synthetic sales dataset and its ground-truth file.

    uv run python task/generate_data.py --seed 42

Writes two files to two different directories on purpose:

  task/data/sales_transactions.csv      the only file an agent is ever given
  evaluation/data/ground_truth.json     answer key, read only by the evaluator

Output is deterministic for a given seed. Anomalies are injected in a fixed
order and every row-level anomaly records the transaction IDs it touched.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

YEAR = 2025
N_BASE_ROWS = 9400  # before thinning and injected extras; final count is ~10,000
N_CUSTOMERS = 900

REGIONS = ["North", "South", "East", "West", "Central"]
REGION_WEIGHTS = np.array([0.24, 0.22, 0.20, 0.20, 0.14])

CATEGORIES = ["Electronics", "Furniture", "Office Supplies", "Apparel", "Home & Garden"]
CATEGORY_WEIGHTS = np.array([0.17, 0.15, 0.28, 0.22, 0.18])
PRICE_RANGE = {  # unit price range per category
    "Electronics": (80, 900),
    "Furniture": (60, 600),
    "Office Supplies": (3, 60),
    "Apparel": (15, 150),
    "Home & Garden": (10, 200),
}
QTY_LAMBDA = {  # quantity = 1 + Poisson(lambda)
    "Electronics": 0.4,
    "Furniture": 0.3,
    "Office Supplies": 4.0,
    "Apparel": 1.5,
    "Home & Garden": 1.0,
}
PRODUCTS_PER_CATEGORY = 8
NEW_PRODUCT_ID = "P041"  # Electronics, launched 1 Sep (legitimate pattern L03)
NEW_PRODUCT_LAUNCH = pd.Timestamp(f"{YEAR}-09-01")

MONTH_VOLUME = np.array([0.85, 0.80, 0.90, 0.95, 1.00, 1.00, 0.95, 0.95, 1.05, 1.10, 1.30, 1.35])
WEEKDAY_VOLUME = np.array([1.10, 1.10, 1.00, 1.00, 1.05, 0.85, 0.75])  # Monday = 0

DISCOUNTS = np.array([0, 5, 10, 15, 20])
DISCOUNT_P = np.array([0.55, 0.15, 0.15, 0.10, 0.05])

UNUSUAL_CUSTOMER = "C0777"
COLUMNS = [
    "transaction_id", "date", "customer_id", "product_id", "category",
    "region", "quantity", "unit_price", "discount_pct", "revenue",
]


def d(month: int, day: int) -> pd.Timestamp:
    return pd.Timestamp(year=YEAR, month=month, day=day)


def build_catalog(rng: np.random.Generator) -> pd.DataFrame:
    rows = []
    n = 0
    for cat in CATEGORIES:
        lo, hi = PRICE_RANGE[cat]
        for _ in range(PRODUCTS_PER_CATEGORY):
            n += 1
            rows.append((f"P{n:03d}", cat, round(float(rng.uniform(lo, hi)), 2)))
    lo, hi = PRICE_RANGE["Electronics"]
    rows.append((NEW_PRODUCT_ID, "Electronics", round(float(rng.uniform(lo, hi)), 2)))
    cat = pd.DataFrame(rows, columns=["product_id", "category", "base_price"])
    cat["launch"] = pd.Timestamp(f"{YEAR}-01-01")
    cat.loc[cat.product_id == NEW_PRODUCT_ID, "launch"] = NEW_PRODUCT_LAUNCH
    cat["popularity"] = rng.lognormal(0.0, 0.5, len(cat))
    return cat


def build_customers(rng: np.random.Generator) -> pd.DataFrame:
    weights = rng.lognormal(0.0, 1.0, N_CUSTOMERS)
    weights[int(UNUSUAL_CUSTOMER[1:]) - 1] = np.median(weights)  # ordinary until October
    regions = rng.choice(REGIONS, N_CUSTOMERS, p=REGION_WEIGHTS)
    return pd.DataFrame({
        "customer_id": [f"C{i + 1:04d}" for i in range(N_CUSTOMERS)],
        "home_region": regions,
        "weight": weights / weights.sum(),
    })


def month_category_matrix() -> np.ndarray:
    """Row per month, column per category: probability a sale falls in that category."""
    m = np.tile(CATEGORY_WEIGHTS, (12, 1))
    m[[3, 4], 4] *= 2.0      # Home & Garden spring surge, Apr-May (L02)
    m[[11, 0], 4] *= 0.6     # Home & Garden quiet in Dec-Jan
    m[[10, 11], 0] *= 1.5    # Electronics holiday demand
    m[[9, 10], 3] *= 1.3     # Apparel autumn demand
    return m / m.sum(axis=1, keepdims=True)


def draw_rows(rng, dates, catalog, customers, matrix, *, disc_values=DISCOUNTS,
              disc_p=DISCOUNT_P, customer_id=None, product_id=None, qty=None):
    """Draw transactions (without IDs or revenue) for the given dates."""
    dates = pd.DatetimeIndex(dates)
    n = len(dates)
    month = dates.month.values - 1

    if product_id is None:
        cum = matrix[month].cumsum(axis=1)
        cat_idx = np.minimum((rng.random(n)[:, None] > cum).sum(axis=1), len(CATEGORIES) - 1)
        categories = np.array(CATEGORIES)[cat_idx]
        products = []
        for dt, cat in zip(dates, categories):
            pool = catalog[(catalog.category == cat) & (catalog.launch <= dt)]
            p = pool.popularity.values / pool.popularity.sum()
            pick = min(int((rng.random() > p.cumsum()).sum()), len(pool) - 1)
            products.append(pool.product_id.values[pick])
        products = np.array(products)
    else:
        products = np.full(n, product_id)
        categories = np.full(n, catalog.set_index("product_id").category[product_id])

    if customer_id is None:
        cust_idx = rng.choice(len(customers), n, p=customers.weight.values)
    else:
        cust_idx = np.full(n, int(customer_id[1:]) - 1)
    home = customers.home_region.values[cust_idx]
    elsewhere = rng.choice(REGIONS, n, p=REGION_WEIGHTS)
    region = np.where(rng.random(n) < 0.85, home, elsewhere)

    base = catalog.set_index("product_id").base_price[products].values
    unit_price = np.round(base * rng.lognormal(0.0, 0.02, n), 2)
    if qty is None:
        lam = np.array([QTY_LAMBDA[c] for c in categories])
        quantity = 1 + rng.poisson(lam)
    else:
        quantity = qty(rng, n)
    discount = rng.choice(disc_values, n, p=disc_p)
    return pd.DataFrame({
        "date": dates, "customer_id": customers.customer_id.values[cust_idx],
        "product_id": products, "category": categories, "region": region,
        "quantity": quantity, "unit_price": unit_price, "discount_pct": discount,
    })


def revenue_of(df: pd.DataFrame) -> pd.Series:
    return (df.quantity * df.unit_price * (1 - df.discount_pct / 100)).round(2)


def generate(seed: int = 42):
    rng = np.random.default_rng(seed)
    catalog = build_catalog(rng)
    customers = build_customers(rng)
    matrix = month_category_matrix()
    args = (catalog, customers, matrix)

    days = pd.date_range(f"{YEAR}-01-01", f"{YEAR}-12-31")
    day_w = MONTH_VOLUME[days.month - 1] * WEEKDAY_VOLUME[days.dayofweek]
    base_dates = days[rng.choice(len(days), N_BASE_ROWS, p=day_w / day_w.sum())]
    base = draw_rows(rng, base_dates, *args)
    base["tag"] = ""

    # A01: West revenue declines for ~2.5 months (rows silently disappear)
    west = (base.region == "West") & base.date.between(d(8, 1), d(10, 15))
    base = base[~(west & (rng.random(len(base)) < 0.45))]
    # A09: Apparel sales drop for four weeks
    apparel = (base.category == "Apparel") & base.date.between(d(2, 10), d(3, 9))
    base = base[~(apparel & (rng.random(len(base)) < 0.65))]
    # A02: two products discounted far beyond the norm for a month
    a02_products = ["P003", "P012"]
    hot = base.product_id.isin(a02_products) & base.date.between(d(3, 15), d(4, 14))
    base.loc[hot, "discount_pct"] = rng.choice([40, 50, 60], int(hot.sum()))
    base.loc[hot, "tag"] = "A02"

    extras = []
    # L01 (legitimate): Black Friday week, more sales and deeper discounts
    bf = draw_rows(rng, pd.date_range(d(11, 24), d(11, 30))[rng.integers(0, 7, 350)], *args,
                   disc_values=np.array([10, 15, 20, 25, 30]),
                   disc_p=np.array([0.20, 0.25, 0.25, 0.15, 0.15]))
    bf["tag"] = "L01"
    extras.append(bf)
    # A03: three-day spike in transaction volume
    spike = draw_rows(rng, pd.date_range(d(6, 14), d(6, 16))[rng.integers(0, 3, 350)], *args)
    spike["tag"] = "A03"
    extras.append(spike)
    # A08: one customer places a burst of small, heavily discounted orders in October
    cust = draw_rows(rng, pd.date_range(d(10, 1), d(10, 31))[rng.integers(0, 31, 150)], *args,
                     disc_values=np.array([30]), disc_p=np.array([1.0]),
                     customer_id=UNUSUAL_CUSTOMER, product_id="P020",
                     qty=lambda r, n: r.integers(1, 3, n))
    cust["tag"] = "A08"
    extras.append(cust)

    df = pd.concat([base, *extras], ignore_index=True)
    df = df.sort_values("date", kind="mergesort").reset_index(drop=True)
    df.insert(0, "transaction_id", [f"T{i + 1:06d}" for i in range(len(df))])
    df["revenue"] = revenue_of(df)

    # Row-level defects, each drawn from rows not already part of another anomaly
    pool = list(df.index[df.tag == ""])

    def take(k):
        idx = sorted(rng.choice(pool, k, replace=False).tolist())
        for i in idx:
            pool.remove(i)
        return idx

    ids = lambda idx: df.loc[idx, "transaction_id"].tolist()
    rows = {}

    rows["A04"] = take(25)  # revenue disagrees with quantity * price * (1 - discount)
    factor = np.where(rng.random(25) < 0.5, rng.uniform(0.4, 0.6, 25), rng.uniform(1.5, 3.0, 25))
    df.loc[rows["A04"], "revenue"] = (df.loc[rows["A04"], "revenue"] * factor).round(2)

    rows["A05"] = take(6)  # implausibly large orders (self-consistent revenue)
    df.loc[rows["A05"], "quantity"] = rng.integers(400, 900, 6)
    df.loc[rows["A05"], "revenue"] = revenue_of(df.loc[rows["A05"]])

    rows["A06"] = take(40)  # missing category
    df["category"] = df["category"].astype(object)
    df.loc[rows["A06"], "category"] = np.nan

    rows["A07"] = take(50)  # category disagrees with the product's category
    for i in rows["A07"][:30]:
        df.at[i, "category"] = rng.choice([c for c in CATEGORIES if c != df.at[i, "category"]])
    for i in rows["A07"][30:]:  # same category, inconsistent spelling
        c = df.at[i, "category"]
        df.at[i, "category"] = rng.choice([c.lower(), c.upper(), c + " "])

    rows["A11"] = take(12)  # negative quantity and revenue: returns or errors
    df.loc[rows["A11"], "quantity"] *= -1
    df.loc[rows["A11"], "revenue"] = revenue_of(df.loc[rows["A11"]])

    dup_src = take(15)  # exact duplicate rows, same transaction_id
    rows["A10"] = dup_src
    row_ids = {k: ids(v) for k, v in rows.items()}  # capture before the re-sort below changes the index
    dups = df.loc[dup_src]
    df = pd.concat([df, dups]).sort_values(["date", "transaction_id"], kind="mergesort")
    df = df.reset_index(drop=True)

    tagged = {t: df.loc[df.tag == t, "transaction_id"].tolist() for t in ("A02", "A03", "A08", "L01")}
    out = df[COLUMNS].copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    truth = build_ground_truth(seed, out, row_ids, tagged, a02_products)
    return out, truth


def build_ground_truth(seed, out, row_ids, tagged, a02_products):
    def anomaly(aid, kind, title, description, parameters, match, transaction_ids=None):
        item = {"id": aid, "kind": kind, "title": title, "description": description,
                "parameters": parameters, "match": match}
        if transaction_ids is not None:
            item["transaction_ids"] = transaction_ids
        return item

    kw = lambda all_=(), any_=(): {k: v for k, v in (("keywords_all", list(all_)), ("keywords_any", list(any_))) if v}
    category = ["categor"]
    anomalies = [
        anomaly("A01", "injected_anomaly", "West region revenue decline",
                "About 45% of West-region transactions were removed between 1 Aug and 15 Oct.",
                {"region": "West", "start": f"{YEAR}-08-01", "end": f"{YEAR}-10-15", "removed_fraction": 0.45},
                [{"segment": {"region": "West"}, "date_overlap": [f"{YEAR}-08-01", f"{YEAR}-10-15"]},
                 {"segment": {"region": "West"}, **kw(any_=["decline", "drop", "decrease", "fall", "dip", "lower"])},
                 kw(["west"], ["decline", "drop", "decrease", "fall", "dip"])]),
        anomaly("A02", "injected_anomaly", "Extreme discounts on two products",
                "P003 and P012 carry 40-60% discounts between 15 Mar and 14 Apr; the norm is 0-20%.",
                {"product_ids": a02_products, "start": f"{YEAR}-03-15", "end": f"{YEAR}-04-14", "discount_pct": [40, 50, 60]},
                [{"segment": {"product_id": "P003"}}, {"segment": {"product_id": "P012"}},
                 kw(["p003"], ["discount"]), kw(["p012"], ["discount"])],
                tagged["A02"]),
        anomaly("A03", "injected_anomaly", "Three-day transaction volume spike",
                "About 350 extra transactions on 14-16 Jun, roughly 5x a normal day.",
                {"start": f"{YEAR}-06-14", "end": f"{YEAR}-06-16", "extra_rows": 350},
                [{"date_overlap": [f"{YEAR}-06-14", f"{YEAR}-06-16"], "max_span_days": 10, **kw(any_=["spike", "surge", "volume", "increase", "jump"])},
                 kw(["june"], ["spike", "surge", "volume"])],
                tagged["A03"]),
        anomaly("A04", "injected_defect", "Revenue inconsistent with quantity, price and discount",
                "25 rows whose recorded revenue is 0.4-0.6x or 1.5-3x of quantity * unit_price * (1 - discount).",
                {"rows": 25},
                [{"transaction_ids_overlap_min": 3},
                 kw(["revenue"], ["mismatch", "inconsisten", "discrepan", "does not match", "doesn't match", "incorrect", "recalcul"])],
                row_ids["A04"]),
        anomaly("A05", "injected_anomaly", "Suspiciously large transactions",
                "6 transactions with quantity 400-899, far above any normal order.",
                {"rows": 6, "quantity_range": [400, 899]},
                [{"transaction_ids_overlap_min": 2},
                 kw(any_=["large transaction", "large order", "outlier", "bulk", "extreme quantity", "unusually large", "very large", "huge"])],
                row_ids["A05"]),
        anomaly("A06", "injected_defect", "Missing product categories",
                "40 rows have an empty category.",
                {"rows": 40},
                [{"transaction_ids_overlap_min": 3}, kw(category, ["missing", "null", "blank", "empty", "nan", "absent"])],
                row_ids["A06"]),
        anomaly("A07", "injected_defect", "Inconsistent product categories",
                "30 rows carry a category that differs from the product's usual category; 20 more use inconsistent case or whitespace.",
                {"mismatched_rows": 30, "formatting_variants": 20},
                [{"transaction_ids_overlap_min": 3},
                 kw(category, ["inconsistent", "mismatch", "misclassif", "mislabel", "variant", "casing", "whitespace", "wrong", "differs", "conflict"])],
                row_ids["A07"]),
        anomaly("A08", "injected_anomaly", "Customer with unusual purchasing behaviour",
                "Customer C0777 places about 150 small orders of product P020 in October, all at 30% discount.",
                {"customer_id": UNUSUAL_CUSTOMER, "product_id": "P020", "rows": 150},
                [{"segment": {"customer_id": UNUSUAL_CUSTOMER}}, kw(["c0777"])],
                tagged["A08"]),
        anomaly("A09", "injected_anomaly", "Temporary drop in Apparel sales",
                "About 65% of Apparel transactions were removed between 10 Feb and 9 Mar.",
                {"category": "Apparel", "start": f"{YEAR}-02-10", "end": f"{YEAR}-03-09", "removed_fraction": 0.65},
                [{"segment": {"category": "Apparel"}, "date_overlap": [f"{YEAR}-02-10", f"{YEAR}-03-09"]},
                 {"segment": {"category": "Apparel"}, **kw(any_=["decline", "drop", "decrease", "fall", "dip", "lower"])},
                 kw(["apparel"], ["decline", "drop", "decrease", "fall", "dip"])]),
        anomaly("A10", "injected_defect", "Duplicate transactions",
                "15 transactions appear twice with the same transaction_id and identical values.",
                {"rows": 15},
                [{"transaction_ids_overlap_min": 3}, kw(any_=["duplicate", "duplicated", "repeated transaction_id", "twice"])],
                row_ids["A10"]),
        anomaly("A11", "ambiguous", "Negative quantity and revenue",
                "12 rows with negative quantity and revenue. These could be returns or entry errors; either reading is defensible.",
                {"rows": 12},
                [{"transaction_ids_overlap_min": 3}, kw(any_=["negative", "return", "refund"])],
                row_ids["A11"]),
        anomaly("L01", "legitimate_pattern", "Black Friday week surge",
                "More sales and deeper discounts (10-30%) on 24-30 Nov. Genuine business seasonality, not a defect.",
                {"start": f"{YEAR}-11-24", "end": f"{YEAR}-11-30"},
                [{"date_overlap": [f"{YEAR}-11-24", f"{YEAR}-11-30"], "max_span_days": 14}, kw(["black friday"])],
                tagged["L01"]),
        anomaly("L02", "legitimate_pattern", "Home & Garden spring seasonality",
                "Home & Garden share doubles in April and May. Genuine seasonality.",
                {"category": "Home & Garden", "start": f"{YEAR}-04-01", "end": f"{YEAR}-05-31"},
                [{"segment": {"category": "Home & Garden"}, "date_overlap": [f"{YEAR}-04-01", f"{YEAR}-05-31"]},
                 kw(["home & garden"], ["spring", "seasonal"])]),
        anomaly("L03", "legitimate_pattern", "New product launch",
                "P041 first sells on 1 Sep because it launched then. Not missing data.",
                {"product_id": NEW_PRODUCT_ID, "launch": f"{YEAR}-09-01"},
                [{"segment": {"product_id": NEW_PRODUCT_ID}}, kw([NEW_PRODUCT_ID.lower()])]),
    ]
    return {
        "seed": seed,
        "row_count": len(out),
        "dataset_sha256": hashlib.sha256(to_csv_bytes(out)).hexdigest(),
        "anomalies": anomalies,
    }


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, float_format="%.2f", lineterminator="\n").encode("utf-8")


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--data-out", type=Path, default=here / "data" / "sales_transactions.csv")
    ap.add_argument("--truth-out", type=Path, default=here.parent / "evaluation" / "data" / "ground_truth.json")
    a = ap.parse_args()

    df, truth = generate(a.seed)
    a.data_out.parent.mkdir(parents=True, exist_ok=True)
    a.truth_out.parent.mkdir(parents=True, exist_ok=True)
    a.data_out.write_bytes(to_csv_bytes(df))
    a.truth_out.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(df)} rows to {a.data_out}")
    print(f"wrote {len(truth['anomalies'])} ground-truth entries to {a.truth_out}")
    print(f"dataset sha256 {truth['dataset_sha256']}")


if __name__ == "__main__":
    main()
