"""The committed results summary must be numbers and statuses only."""

import json

from experiments.export_results import export_experiment, export_scenarios, main

SECRET_TEXT = "sk-ant-api03-SHOULDNEVERAPPEAR sesn_01ABCDEF agent_01ABCDEF the agent wrote this sentence"


def report():
    metrics = {"implementation": "managed", "run": "rep-1", "model": "claude-sonnet-5-5", "status": "completed",
               "completed": True, "active_seconds": 12.5, "model_calls": 3, "tool_calls": 4, "input_tokens": 1,
               "output_tokens": 2, "cache_read_tokens": 3, "cache_write_tokens": 4, "peak_context_tokens": None,
               "infrastructure_errors": 0, "token_cost_usd": 0.1, "runtime_cost_usd": 0.001, "total_cost_usd": 0.101,
               "reported_list_cost_usd": 0.1, "artifact_files": 8, "session_id": "sesn_01ABCDEF", "final_message": SECRET_TEXT}
    evaluation = {"schema_valid": True, "required_artifacts_complete": True, "finding_count": 12, "run": "/abs/path",
                  "schema_errors": [SECRET_TEXT], "artifacts": {"chart_count": 5},
                  "analysis_reproduction": {"ok": True, "stderr_tail": SECRET_TEXT},
                  "detection": {"injected_total": 10, "injected_detected": 9, "injected_recall": 0.9, "detected": ["A01"],
                                "missed": ["A09"], "ambiguous_detected": [], "legit_patterns_mentioned": ["L01"],
                                "unmatched_findings": ["F1", "F2"], "legit_patterns_flagged_as_problems": [],
                                "matches": {"A01": ["F3"]}},
                  "numeric_claims": {"available": True, "supported": 4, "unsupported": 1, "unverifiable": 30,
                                     "checked_fraction": 0.14, "unsupported_details": [{"metric": SECRET_TEXT}]}}
    return {"aggregate": {"managed": {"runs": 1}}, "runs": [{"metrics": metrics, "evaluation": evaluation}]}


def test_experiment_export_keeps_numbers_and_drops_text_and_ids():
    out = export_experiment(report())
    run = out["runs"][0]
    assert run["detection"]["unmatched_findings"] == 2 and run["detection"]["injected_recall"] == 0.9
    assert run["analysis_py_runs"] is True and run["numeric_claims"]["supported"] == 4
    text = json.dumps(out)
    for forbidden in ("SHOULDNEVERAPPEAR", "sesn_", "agent_01", "wrote this sentence", "/abs/path", "final_message",
                      "session_id", "schema_errors", "stderr", "matches", "unsupported_details"):
        assert forbidden not in text, forbidden


def test_scenario_export_drops_the_agents_words():
    out = export_scenarios({"syntax_error": {
        "diy": {"recorded": True, "status": "completed", "result_txt": "OK", "expected_result": "OK", "model_calls": 3,
                "tool_calls": 2, "active_seconds": 6.6, "cost_usd": 0.006, "infrastructure_errors": 0,
                "final_message": SECRET_TEXT},
        "managed": {"not_applicable": "text that should not be copied"}},
        "client_interrupted": {"diy": {"status": "completed", "interruption": {"killed_after_tool_calls": 6}}}})
    assert out["syntax_error"]["diy"]["result_txt"] == "OK"
    assert out["syntax_error"]["managed"] == {"not_applicable": True}
    assert out["client_interrupted"]["diy"]["killed_after_tool_calls"] == 6
    text = json.dumps(out)
    assert "SHOULDNEVERAPPEAR" not in text and "final_message" not in text and "should not be copied" not in text


def test_files_are_written(tmp_path):
    exp, fail = tmp_path / "exp", tmp_path / "fail"
    exp.mkdir()
    fail.mkdir()
    (exp / "report.json").write_text(json.dumps(report()))
    (exp / "report.md").write_text("# table\n")
    (fail / "failure-report.json").write_text(json.dumps({"syntax_error": {}}))
    (fail / "failure-report.md").write_text("# scenarios\n")
    assert main(["--experiment", str(exp), "--failures", str(fail), "--out", str(tmp_path / "out")]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "experiment-1.json", "experiment-1.md", "failure-scenarios.json", "failure-scenarios.md"]
