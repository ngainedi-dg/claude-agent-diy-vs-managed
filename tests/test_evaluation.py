import json
import re

import pandas as pd
import pytest
from jsonschema import Draft202012Validator

from evaluation.evaluate import (SCHEMA_PATH, canonical_metric, check_numeric_claims, evaluate_run, find_runs)
from evaluation.ground_truth import finding_matches, rule_matches, score_findings
from task.generate_data import generate
from tests.conftest import ROOT


@pytest.fixture(scope="module")
def truth():
    return generate(42)[1]


@pytest.fixture(scope="module")
def validator():
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def finding(fid="F1", description="x", segment=None, further=False, **kw):
    return {"finding_id": fid, "description": description, "segment": segment or {}, "evidence": [],
            "confidence": "medium", "further_investigation_recommended": further, **kw}


def entry(truth, aid):
    return next(e for e in truth["anomalies"] if e["id"] == aid)


# --- rule matching ---------------------------------------------------------

def test_segment_rule_is_case_insensitive():
    assert rule_matches({"segment": {"region": "West"}}, finding(segment={"region": "west"}))
    assert not rule_matches({"segment": {"region": "West"}}, finding(segment={"region": "East"}))
    assert not rule_matches({"segment": {"region": "West"}}, finding())


def test_date_overlap_and_span():
    rule = {"date_overlap": ["2025-06-14", "2025-06-16"], "max_span_days": 10}
    assert rule_matches(rule, finding(segment={"date_range": ["2025-06-10", "2025-06-20"]}))
    assert not rule_matches(rule, finding(segment={"date_range": ["2025-07-01", "2025-07-05"]}))
    assert not rule_matches(rule, finding(segment={"date_range": ["2025-01-01", "2025-12-31"]}))
    assert not rule_matches(rule, finding())


def test_keywords_all_and_any():
    rule = {"keywords_all": ["revenue"], "keywords_any": ["mismatch", "discrepan"]}
    assert rule_matches(rule, finding(description="Revenue mismatch in 25 rows"))
    assert not rule_matches(rule, finding(description="Revenue is fine"))
    assert not rule_matches(rule, finding(description="a mismatch of something else"))


def test_keywords_search_evidence_and_uncertainty():
    f = finding(description="odd", uncertainty="could be a duplicate load")
    assert rule_matches({"keywords_any": ["duplicate"]}, f)


def test_transaction_id_overlap(truth):
    a10 = entry(truth, "A10")
    ids = a10["transaction_ids"]
    assert finding_matches(a10, finding(segment={"transaction_ids": ids[:3]}))
    assert not finding_matches(a10, finding(segment={"transaction_ids": ids[:2]}))


# --- scoring ---------------------------------------------------------------

def perfect_findings(truth):
    out = []
    for e in truth["anomalies"]:
        if e["kind"] == "legitimate_pattern":
            continue
        ids = e.get("transaction_ids", [])
        out.append(finding(fid=e["id"], description=e["title"], segment={"transaction_ids": ids[:5]} if ids else {}))
    return out


def test_empty_findings_detect_nothing(truth):
    s = score_findings([], truth)
    assert s["injected_detected"] == 0 and s["injected_recall"] == 0.0
    assert set(s["missed"]) == {e["id"] for e in truth["anomalies"] if e["kind"].startswith("injected")}


def test_rich_findings_are_detected(truth):
    findings = [
        finding("F1", "West region revenue dropped sharply", {"region": "West", "date_range": ["2025-08-01", "2025-10-15"]}),
        finding("F2", "Products P003 and P012 sold at extreme discounts"),
        finding("F3", "Transaction volume spike on mid June", {"date_range": ["2025-06-14", "2025-06-16"]}),
        finding("F4", "Customer C0777 bought P020 repeatedly", {"customer_id": "C0777"}),
        finding("F5", "Apparel sales fell in Feb", {"category": "Apparel", "date_range": ["2025-02-10", "2025-03-09"]}),
        finding("F6", "Missing category values in 40 rows"),
        finding("F7", "Duplicate transaction rows", {"transaction_ids": entry(truth, "A10")["transaction_ids"][:5]}),
    ]
    s = score_findings(findings, truth)
    assert {"A01", "A02", "A03", "A06", "A08", "A09", "A10"} <= set(s["detected"])
    assert s["unmatched_findings"] == []


def test_a_short_window_on_the_spike_dates_matches_whatever_word_the_agent_used(truth):
    # Found in a real run: "burst" was not among the keywords, so a correct finding was scored as a miss.
    f = finding("F9", "A three-day burst: 406 transactions vs a median of 25 a day", {"date_range": ["2025-06-14", "2025-06-16"]})
    assert "A03" in score_findings([f], truth)["detected"]
    wide = finding("F9", "June was busy", {"date_range": ["2025-06-01", "2025-06-30"]})
    assert "A03" not in score_findings([wide], truth)["detected"]


def test_unmatched_finding_is_reported(truth):
    s = score_findings([finding("F9", "Tuesdays are slightly busier than Wednesdays")], truth)
    assert s["unmatched_findings"] == ["F9"]


def test_legit_pattern_flagged_only_when_investigation_requested(truth):
    seg = {"date_range": ["2025-11-24", "2025-11-30"]}
    quiet = score_findings([finding("F1", "Black Friday week surge", seg, further=False)], truth)
    loud = score_findings([finding("F1", "Black Friday week surge", seg, further=True)], truth)
    assert quiet["legit_patterns_mentioned"] == ["L01"] and quiet["legit_patterns_flagged_as_problems"] == []
    assert loud["legit_patterns_flagged_as_problems"] == ["F1"]


def test_ambiguous_entry_does_not_count_toward_recall(truth):
    f = finding("F1", "Negative quantities look like returns", {"transaction_ids": entry(truth, "A11")["transaction_ids"][:4]})
    s = score_findings([f], truth)
    assert s["ambiguous_detected"] == ["A11"]
    assert "A11" not in s["detected"] and s["injected_detected"] == 0


# --- schema ----------------------------------------------------------------

def instructions_example():
    text = (ROOT / "task" / "instructions.md").read_text(encoding="utf-8")
    return json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))


def test_instructions_example_is_valid(validator):
    assert list(validator.iter_errors(instructions_example())) == []


@pytest.mark.parametrize("drop", ["finding_id", "description", "segment", "evidence", "confidence",
                                  "further_investigation_recommended"])
def test_required_finding_fields(validator, drop):
    doc = instructions_example()
    del doc["findings"][0][drop]
    assert list(validator.iter_errors(doc))


def test_bad_confidence_and_dates_rejected(validator):
    doc = instructions_example()
    doc["findings"][0]["confidence"] = "certain"
    doc["findings"][0]["segment"]["date_range"] = ["Aug 1", "Oct 15"]
    assert len(list(validator.iter_errors(doc))) == 3  # bad enum, two malformed dates


# --- numeric claims --------------------------------------------------------

DF = pd.DataFrame({"transaction_id": ["T1", "T2", "T2"], "revenue": [100.0, 200.0, 200.0],
                   "category": ["A", None, None], "quantity": [1, -1, -1],
                   "customer_id": ["C1", "C2", "C2"], "product_id": ["P1", "P1", "P1"]})


def test_segment_scoped_findings_are_not_checked_against_the_whole_file():
    # Found in the first real run: "row_count: 6" in a finding about six transactions is not a claim about the file.
    scoped = finding(segment={"transaction_ids": ["T1"]}, evidence=[{"metric": "row_count", "value": 6}])
    r = check_numeric_claims([scoped], DF)
    assert (r["verified_correct"], r["verified_incorrect"], r["unverifiable"]) == (0, 0, 1)


def test_numeric_claims_split_correct_incorrect_unverifiable():
    f = finding(evidence=[
        {"metric": "Total Revenue", "value": 500.0},       # correct
        {"metric": "row_count", "value": 10},               # incorrect
        {"metric": "west_share_pct", "value": 22.5},        # unverifiable
        {"metric": "missing_category_count", "value": "2"}, # string -> unverifiable
    ])
    r = check_numeric_claims([f], DF)
    assert (r["verified_correct"], r["verified_incorrect"], r["unverifiable"]) == (1, 1, 2)


def test_numeric_claims_unavailable_without_dataset():
    assert check_numeric_claims([finding()], None) == {"available": False}


def test_metric_aliases():
    assert canonical_metric("Transaction Count") == "row_count"
    assert canonical_metric("total-revenue") == "total_revenue"


# --- whole runs ------------------------------------------------------------

def make_run(path, summary=None, charts=2, report="## Limitations\nSome uncertainty. Recommended follow-up."):
    path.mkdir(parents=True)
    (path / "report.md").write_text(report, encoding="utf-8")
    (path / "analysis.py").write_text("print('hi')\n", encoding="utf-8")
    (path / "charts").mkdir()
    for i in range(charts):
        (path / "charts" / f"c{i}.png").write_bytes(b"\x89PNG")
    (path / "summary.json").write_text(json.dumps(summary if summary is not None else instructions_example()),
                                       encoding="utf-8")


def test_complete_run(tmp_path, truth, validator):
    run = tmp_path / "diy" / "run1"
    make_run(run)
    r = evaluate_run(run, truth, DF, validator)
    assert r["required_artifacts_complete"] and r["schema_valid"]
    assert r["artifacts"]["report_mentions_uncertainty"] and r["artifacts"]["report_mentions_follow_up"]
    assert find_runs(tmp_path) == [run]


def test_incomplete_runs(tmp_path, truth, validator):
    few_charts = tmp_path / "a"
    make_run(few_charts, charts=1)
    assert not evaluate_run(few_charts, truth, DF, validator)["required_artifacts_complete"]

    bad = tmp_path / "b"
    make_run(bad, summary={"findings": [{"finding_id": "F1"}]})
    r = evaluate_run(bad, truth, DF, validator)
    assert not r["schema_valid"] and r["schema_errors"] and "detection" not in r

    broken = tmp_path / "c"
    make_run(broken)
    (broken / "summary.json").write_text("{not json", encoding="utf-8")
    assert "not valid JSON" in evaluate_run(broken, truth, DF, validator)["schema_errors"][0]
