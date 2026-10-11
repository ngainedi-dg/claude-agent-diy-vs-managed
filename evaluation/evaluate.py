"""Score agent outputs. Independent of both agent implementations.

    uv run python -m evaluation.evaluate --results results/

A run directory is any directory under --results that contains report.md,
analysis.py, summary.json or charts/. The evaluator writes evaluation.json
into each run directory and prints a summary table.

It never executes agent-written code. Whether analysis.py runs is checked
separately, inside a sandbox (Phase 5).
"""

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd
from jsonschema import Draft202012Validator

from evaluation.ground_truth import DEFAULT_PATH, load_ground_truth, score_findings

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "task" / "summary.schema.json"
DATASET_PATH = ROOT / "task" / "data" / "sales_transactions.csv"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg"}
RUN_MARKERS = ("report.md", "analysis.py", "summary.json", "charts")


def recompute_metrics(df: pd.DataFrame) -> dict[str, float]:
    """Numbers the evaluator can recompute from the raw CSV, keyed by canonical metric name."""
    return {
        "total_revenue": float(df.revenue.sum()),
        "row_count": float(len(df)),
        "unique_transaction_ids": float(df.transaction_id.nunique()),
        "duplicate_transaction_ids": float(len(df) - df.transaction_id.nunique()),
        "duplicate_rows": float(df.duplicated().sum()),
        "missing_category_count": float(df.category.isna().sum()),
        "negative_quantity_count": float((df.quantity < 0).sum()),
        "unique_customers": float(df.customer_id.nunique()),
        "unique_products": float(df.product_id.nunique()),
    }


METRIC_ALIASES = {
    "transaction_count": "row_count", "num_transactions": "row_count", "total_transactions": "row_count",
    "rows": "row_count", "total_rows": "row_count",
    "total_recorded_revenue": "total_revenue", "revenue_total": "total_revenue",
    "duplicate_row_count": "duplicate_rows", "duplicate_count": "duplicate_transaction_ids",
    "missing_category_rows": "missing_category_count", "null_category_count": "missing_category_count",
    "negative_quantity_rows": "negative_quantity_count",
    "customer_count": "unique_customers", "product_count": "unique_products",
}


def canonical_metric(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return METRIC_ALIASES.get(key, key)


def check_numeric_claims(findings: list[dict], df: pd.DataFrame | None) -> dict:
    """Compare evidence values the evaluator can recompute. Everything else is unverifiable, not wrong."""
    if df is None:
        return {"available": False}
    truth = recompute_metrics(df)
    correct, incorrect, unverifiable = [], [], 0
    for f in findings:
        for item in f.get("evidence", []):
            key = canonical_metric(str(item.get("metric", "")))
            value = item.get("value")
            if key not in truth or isinstance(value, bool) or not isinstance(value, (int, float)):
                unverifiable += 1
                continue
            tol = max(0.01 * abs(truth[key]), 0.5)
            record = {"finding": f.get("finding_id"), "metric": key, "claimed": value, "actual": truth[key]}
            (correct if abs(value - truth[key]) <= tol else incorrect).append(record)
    return {"available": True, "verified_correct": len(correct), "verified_incorrect": len(incorrect),
            "unverifiable": unverifiable, "incorrect_details": incorrect}


def check_artifacts(run: Path) -> dict:
    report = run / "report.md"
    text = report.read_text(encoding="utf-8", errors="replace") if report.is_file() else ""
    charts = [p for p in (run / "charts").glob("*") if p.suffix.lower() in IMAGE_SUFFIXES] \
        if (run / "charts").is_dir() else []
    return {
        "report_md": bool(text.strip()),
        "analysis_py": (run / "analysis.py").is_file() and (run / "analysis.py").stat().st_size > 0,
        "chart_count": len(charts),
        "charts_ok": len(charts) >= 2,
        "summary_json_present": (run / "summary.json").is_file(),
        "report_mentions_uncertainty": bool(re.search(r"uncertain|limitation|caveat|confidence", text, re.I)),
        "report_mentions_follow_up": bool(re.search(r"follow-up|follow up|next step|recommend", text, re.I)),
    }


def load_summary(run: Path, validator: Draft202012Validator):
    path = run / "summary.json"
    if not path.is_file():
        return None, ["summary.json missing"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, [f"summary.json is not valid JSON: {e}"]
    errors = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in validator.iter_errors(data)]
    return data, errors


def evaluate_run(run: Path, truth: dict, df: pd.DataFrame | None, validator) -> dict:
    artifacts = check_artifacts(run)
    summary, schema_errors = load_summary(run, validator)
    result = {"run": str(run), "artifacts": artifacts, "schema_valid": summary is not None and not schema_errors,
              "schema_errors": schema_errors[:10]}
    findings = (summary or {}).get("findings", [])
    result["finding_count"] = len(findings)
    if summary is not None and not schema_errors:
        result["detection"] = score_findings(findings, truth)
        result["numeric_claims"] = check_numeric_claims(findings, df)
    result["required_artifacts_complete"] = all(
        [artifacts["report_md"], artifacts["analysis_py"], artifacts["charts_ok"], result["schema_valid"]])
    return result


def find_runs(results: Path) -> list[Path]:
    runs = []
    for p in sorted(results.rglob("*")):
        if p.is_dir() and p.name != "charts" and any((p / m).exists() for m in RUN_MARKERS):
            runs.append(p)
    return runs


def print_table(rows: list[dict]) -> None:
    print(f"{'run':<44}{'artifacts':>10}{'schema':>8}{'findings':>9}{'recall':>8}{'missed':>8}{'unmatched':>10}{'legit-flag':>11}")
    for r in rows:
        det = r.get("detection")
        print(f"{r['run'][-43:]:<44}{('ok' if r['required_artifacts_complete'] else 'incomplete'):>10}"
              f"{('ok' if r['schema_valid'] else 'invalid'):>8}{r['finding_count']:>9}"
              f"{(format(det['injected_recall'], '.0%') if det else 'n/a'):>8}"
              f"{(len(det['missed']) if det else 'n/a'):>8}"
              f"{(len(det['unmatched_findings']) if det else 'n/a'):>10}"
              f"{(len(det['legit_patterns_flagged_as_problems']) if det else 'n/a'):>11}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=ROOT / "results")
    ap.add_argument("--ground-truth", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--dataset", type=Path, default=DATASET_PATH)
    ap.add_argument("--no-write", action="store_true", help="do not write evaluation.json files")
    a = ap.parse_args(argv)

    if not a.ground_truth.is_file():
        print(f"ground truth not found at {a.ground_truth}; run task/generate_data.py first", file=sys.stderr)
        return 2
    truth = load_ground_truth(a.ground_truth)
    df = pd.read_csv(a.dataset) if a.dataset.is_file() else None
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))

    runs = find_runs(a.results)
    if not runs:
        print(f"no run directories found under {a.results}")
        return 0
    rows = [evaluate_run(r, truth, df, validator) for r in runs]
    if not a.no_write:
        for r in rows:
            (Path(r["run"]) / "evaluation.json").write_text(json.dumps(r, indent=2) + "\n", encoding="utf-8")
    print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
