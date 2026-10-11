"""Numeric-claim checking, run metrics, and the analysis re-run check."""

import json
import shutil
import sqlite3
import subprocess

import pandas as pd
import pytest

from evaluation import metrics
from evaluation.claims import check_claims, classify_metric, variants
from evaluation.reproduce import run_analysis
from task.generate_data import generate


@pytest.fixture(scope="module")
def df():
    data, _ = generate(42)
    return data


def finding(segment=None, **evidence):
    return {"finding_id": "F1", "description": "x", "segment": segment or {},
            "evidence": [{"metric": k, "value": v} for k, v in evidence.items()]}


# --- which metric names we dare to check ----------------------------------------

@pytest.mark.parametrize("name,segment,kind", [
    ("total_revenue", {}, "revenue"), ("Total Revenue (USD)", {}, "revenue"), ("transaction_count", {}, "count"),
    ("rows", {}, "count"), ("west_revenue", {"region": "West"}, "revenue"), ("p020_orders", {"product_id": "P020"}, "count"),
    ("mean_discount", {}, "discount"), ("units_sold", {}, None),
    # unrecognised qualifiers, shares, rates and medians must stay unverifiable
    ("nov_revenue", {}, None), ("orders_monday", {}, None), ("revenue_share_pct", {}, None),
    ("orders_per_day", {}, None), ("median_quantity", {}, None), ("avg_order_value", {}, None),
    ("revenue_Electronics", {}, None), ("discount_pct", {}, None), ("", {}, None),
])
def test_classify_metric(name, segment, kind):
    assert classify_metric(name, segment) == kind


# --- claims against the real dataset ---------------------------------------------

def counts(result):
    return result["supported"], result["unsupported"], result["unverifiable"]


def test_dataset_wide_claims(df):
    claim = check_claims([finding(total_revenue=float(df.revenue.sum()), row_count=len(df))], df)
    assert counts(claim) == (2, 0, 0)


def test_claims_on_a_segment_are_checked_against_that_segment(df):
    west = df[df.region == "West"]
    assert counts(check_claims([finding({"region": "West"}, revenue=float(west.revenue.sum()), transactions=len(west))], df)) == (2, 0, 0)
    # the same numbers claimed for another region are not supported
    wrong = check_claims([finding({"region": "East"}, revenue=float(west.revenue.sum()))], df)
    assert counts(wrong) == (0, 1, 0) and wrong["unsupported_details"][0]["finding"] == "F1"


def test_a_number_can_describe_a_wider_slice_than_the_segment_lists(df):
    """Found in real runs: a finding scoped to a product and its category counted the product's mis-labelled rows too,
    and a customer finding listed a region but counted all the customer's orders."""
    product = df[df.product_id == "P041"]
    only_product = check_claims([finding({"product_id": "P041", "category": "Electronics"}, orders=len(product))], df)
    assert counts(only_product) == (1, 0, 0)
    customer = df[df.customer_id == "C0765"]
    assert counts(check_claims([finding({"customer_id": "C0765", "region": "East"}, orders=len(customer))], df)) == (1, 0, 0)


def test_a_claim_is_supported_if_it_matches_any_cleaning_variant(df):
    v = variants(df)
    assert len(v["raw"]) > len(v["deduplicated"]) > len(v["cleaned"]) >= len(v["cleaned_no_extremes"])
    for name, frame in v.items():
        assert check_claims([finding(total_revenue=float(frame.revenue.sum()))], df)["supported"] == 1, name


def test_unmatched_numbers_are_unsupported_and_unknown_metrics_are_unverifiable(df):
    r = check_claims([finding(total_revenue=1.0, nov_revenue=5.0, west_share_pct=22.0)], df)
    assert counts(r) == (0, 1, 2)
    assert r["checked_fraction"] == pytest.approx(1 / 3, abs=1e-3)


def test_sample_transaction_ids_alone_cannot_scope_a_claim(df):
    r = check_claims([finding({"transaction_ids": ["T000001"]}, row_count=6)], df)
    assert counts(r) == (0, 0, 1)  # found in the first real run: "6" meant six transactions, not the whole file


def test_date_range_and_category_normalisation(df):
    june = df[(df.date >= "2025-06-14") & (df.date <= "2025-06-16")]
    assert check_claims([finding({"date_range": ["2025-06-14", "2025-06-16"]}, transactions=len(june))], df)["supported"] == 1
    apparel = df[df.category.astype(str).str.strip().str.lower() == "apparel"]
    assert check_claims([finding({"category": " APPAREL "}, transactions=len(apparel))], df)["supported"] == 1


def test_non_numbers_and_missing_dataset():
    frame = pd.DataFrame({"transaction_id": ["T1"], "date": ["2025-01-01"], "customer_id": ["C1"], "product_id": ["P1"],
                          "category": ["A"], "region": ["West"], "quantity": [1], "unit_price": [1.0],
                          "discount_pct": [0], "revenue": [1.0]})
    assert counts(check_claims([finding(total_revenue="1.0", row_count=True)], frame)) == (0, 0, 2)
    assert check_claims([finding()], None) == {"available": False}


# --- run metrics ---------------------------------------------------------------

def write_diy(path, status="completed"):
    path.mkdir(parents=True)
    (path / "run.json").write_text(json.dumps({
        "implementation": "diy", "model": "claude-sonnet-5-5", "status": status, "active_seconds": 100.0,
        "usage": {"input_tokens": 1000, "output_tokens": 2000, "cache_write_tokens": 3000, "cache_read_tokens": 40000,
                  "model_calls": 5, "peak_context_tokens": 9000}}))
    db = sqlite3.connect(path / "session.sqlite")
    db.executescript("CREATE TABLE tool_calls (tool_use_id TEXT); CREATE TABLE errors (id INTEGER);"
                     "INSERT INTO tool_calls VALUES ('a'),('b'),('c'); INSERT INTO errors VALUES (1);")
    db.commit()
    (path / "outputs").mkdir()
    (path / "outputs" / "report.md").write_text("x")
    (path / "outputs" / "_scratch").mkdir()
    (path / "outputs" / "_scratch" / "step_001.py").write_text("x")
    return path


def test_diy_metrics_use_one_price_table_and_leave_unobserved_values_empty(tmp_path):
    m = metrics.load_run_metrics(write_diy(tmp_path / "r"))
    expected = (1000 * 2 + 2000 * 10 + 3000 * 2.5 + 40000 * 0.1) / 1e6
    assert m["token_cost_usd"] == pytest.approx(expected, abs=1e-4)
    assert m["runtime_cost_usd"] is None and m["reported_list_cost_usd"] is None  # not measured: None, never 0
    assert m["total_cost_usd"] == m["token_cost_usd"]
    assert (m["tool_calls"], m["infrastructure_errors"], m["model_calls"], m["artifact_files"]) == (3, 1, 5, 1)
    assert m["completed"] and m["implementation"] == "diy"


def test_managed_metrics_add_runtime_as_a_separate_charge(tmp_path):
    run = tmp_path / "m"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({
        "implementation": "managed", "model": "claude-sonnet-5-5", "status": "budget_exhausted", "errors": ["e"],
        "metrics": {"session_usage": {"input_tokens": 50, "output_tokens": 34022, "cache_read_input_tokens": 807914,
                                      "cache_write_5m_tokens": 67792, "cache_write_1h_tokens": 0},
                    "active_seconds": 294.374, "model_calls": 24, "tool_calls": 27, "list_cost_usd_reported": "0.60"}}))
    m = metrics.load_run_metrics(run)
    assert m["token_cost_usd"] == pytest.approx(0.5906, abs=1e-3) and m["runtime_cost_usd"] == pytest.approx(0.0065, abs=1e-4)
    assert m["total_cost_usd"] == pytest.approx(0.597, abs=2e-3) and m["reported_list_cost_usd"] == 0.60
    assert not m["completed"] and m["infrastructure_errors"] == 1 and m["peak_context_tokens"] is None


def test_missing_usage_gives_none_not_zero(tmp_path):
    run = tmp_path / "m"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({
        "implementation": "managed", "model": "claude-sonnet-5-5", "status": "failed",
        "metrics": {"session_usage": {"input_tokens": None, "output_tokens": None, "cache_read_input_tokens": None,
                                      "cache_write_5m_tokens": None, "cache_write_1h_tokens": None},
                    "active_seconds": None, "list_cost_usd_reported": None}}))
    m = metrics.load_run_metrics(run)
    assert m["token_cost_usd"] is None and m["total_cost_usd"] is None and m["runtime_cost_usd"] is None


def test_price_tables_agree_across_modules():
    import diy.agent_loop
    import managed.main
    assert metrics.PRICES == diy.agent_loop.PRICES == managed.main.PRICES
    assert metrics.RUNTIME_USD_PER_HOUR == managed.main.RUNTIME_USD_PER_HOUR


# --- re-running analysis.py (needs Docker) -------------------------------------

needs_docker = pytest.mark.skipif(not shutil.which("docker") or subprocess.run(
    ["docker", "info"], capture_output=True).returncode != 0, reason="Docker is not available")


@needs_docker
def test_analysis_script_is_run_in_a_container_with_both_data_paths(tmp_path):
    csv = tmp_path / "sales_transactions.csv"
    csv.write_text("a\n1\n2\n")
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "analysis.py").write_text(
        "import os, sys\npaths = ['/data/sales_transactions.csv', '/mnt/session/uploads/sales_transactions.csv']\n"
        "assert all(os.path.exists(p) for p in paths)\n"
        "os.makedirs('/mnt/session/outputs/charts', exist_ok=True)\n"
        "open('/mnt/session/outputs/charts/c.png', 'w').write('x')\n"
        "try:\n    open('/etc/pwned', 'w')\nexcept OSError:\n    print('read-only ok')\n")
    r = run_analysis(out, csv, timeout=60)
    assert r["ok"] and r["files_produced"] == 1
    assert not (out / "charts").exists()  # ran in a scratch copy; the results directory is untouched


@needs_docker
def test_failing_hanging_and_missing_scripts_are_reported(tmp_path):
    csv = tmp_path / "sales_transactions.csv"
    csv.write_text("a\n1\n")
    out = tmp_path / "outputs"
    out.mkdir()
    assert run_analysis(out, csv)["reason"] == "analysis.py missing"
    (out / "analysis.py").write_text("raise SystemExit('boom')\n")
    broken = run_analysis(out, csv, timeout=60)
    assert broken["ok"] is False and "boom" in broken["stderr_tail"]
    (out / "analysis.py").write_text("import time; time.sleep(120)\n")
    hung = run_analysis(out, csv, timeout=3)
    assert hung["ok"] is False and hung["timed_out"] is True and hung["duration_s"] < 30
