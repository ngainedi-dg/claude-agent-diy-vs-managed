"""The experiment runner and the failure-scenario runner. No API calls: runs are faked on disk."""

import json
import re
import sqlite3
from argparse import Namespace

import pytest

import experiments.failure_scenarios as fs
import experiments.run_comparison as rc
from task.generate_data import generate
from tests.conftest import ROOT


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    base = tmp_path_factory.mktemp("data")
    df, truth = generate(42)
    (base / "sales_transactions.csv").write_bytes(df.to_csv(index=False, float_format="%.2f").encode())
    (base / "ground_truth.json").write_text(json.dumps(truth))
    return base


@pytest.fixture
def patched(dataset, monkeypatch):
    monkeypatch.setattr(rc, "DATASET_PATH", dataset / "sales_transactions.csv")
    monkeypatch.setattr(rc, "DEFAULT_PATH", dataset / "ground_truth.json")
    monkeypatch.setattr(rc, "docker_ready", lambda: False)


def summary_example():
    text = (ROOT / "task" / "instructions.md").read_text(encoding="utf-8")
    return json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))


def fake_run(run_dir, impl, *, status="completed", cost_tokens=(1000, 2000, 3000, 40000), seconds=100.0, charts=2):
    inp, out, write, read = cost_tokens
    run_dir.mkdir(parents=True)
    if impl == "diy":
        info = {"implementation": "diy", "model": "claude-sonnet-5-5", "status": status, "active_seconds": seconds,
                "usage": {"input_tokens": inp, "output_tokens": out, "cache_write_tokens": write,
                          "cache_read_tokens": read, "model_calls": 5, "peak_context_tokens": 9000}}
        db = sqlite3.connect(run_dir / "session.sqlite")
        db.executescript("CREATE TABLE tool_calls (tool_use_id TEXT, is_error INTEGER); CREATE TABLE errors (id INTEGER);"
                         "INSERT INTO tool_calls VALUES ('a', 0), ('b', 1);")
        db.commit()
        db.close()
    else:
        info = {"implementation": "managed", "model": "claude-sonnet-5-5", "status": status, "errors": [],
                "metrics": {"session_usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                                              "cache_write_5m_tokens": write, "cache_write_1h_tokens": 0},
                            "active_seconds": seconds, "model_calls": 4, "tool_calls": 7, "list_cost_usd_reported": "0.10"}}
    (run_dir / "run.json").write_text(json.dumps(info))
    outputs = run_dir / "outputs"
    (outputs / "charts").mkdir(parents=True)
    (outputs / "report.md").write_text("Limitations and uncertainty. Recommended follow-up.")
    (outputs / "analysis.py").write_text("print(1)")
    (outputs / "summary.json").write_text(json.dumps(summary_example()))
    for i in range(charts):
        (outputs / "charts" / f"{i}.png").write_bytes(b"png")
    return run_dir


# --- the runner -------------------------------------------------------------------

def args(tmp_path, **kw):
    base = dict(repetitions=2, implementations=("diy", "managed"), model="claude-sonnet-5-5", timeout_minutes=30,
                max_iterations=40, max_cost_per_run=3.0, max_experiment_cost=8.0, out=tmp_path, yes=True)
    return Namespace(**{**base, **kw})


def test_paid_runs_need_explicit_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "launch", lambda *a, **k: pytest.fail("launched without --yes"))
    with pytest.raises(SystemExit, match="--yes"):
        rc.run_experiment(args(tmp_path, yes=False))


def test_runs_alternate_who_goes_first_and_finished_runs_are_skipped(tmp_path, monkeypatch):
    order = []

    def fake_launch(impl, run_dir, a):
        order.append((impl, run_dir.name))
        fake_run(run_dir, impl)
        return 0

    monkeypatch.setattr(rc, "launch", fake_launch)
    fake_run(tmp_path / "diy" / "rep-1", "diy")  # already done
    rc.run_experiment(args(tmp_path))
    assert order == [("managed", "rep-1"), ("managed", "rep-2"), ("diy", "rep-2")]
    order.clear()
    rc.run_experiment(args(tmp_path))
    assert order == []  # nothing left to do


def test_experiment_stops_before_exceeding_the_budget(tmp_path, monkeypatch):
    launched = []
    # each fake run really costs $1.00 (100k output tokens), so a $2.50 budget allows two runs and no more
    monkeypatch.setattr(rc, "launch", lambda impl, d, a: (launched.append(impl), fake_run(d, impl, cost_tokens=(0, 100_000, 0, 0)), 0)[2])
    rc.run_experiment(args(tmp_path, repetitions=5, max_experiment_cost=2.5))
    assert len(launched) == 2


def test_a_run_that_really_cost_nothing_is_not_treated_as_unknown(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(rc, "launch", lambda impl, d, a: (launched.append(impl), fake_run(d, impl, cost_tokens=(0, 0, 0, 0)), 0)[2])
    rc.run_experiment(args(tmp_path, repetitions=2, max_experiment_cost=1.0, max_cost_per_run=3.0))
    assert len(launched) == 4  # a $0.00 run adds $0.00, not the $3.00 cap


def test_the_model_flag_must_match_the_managed_agent(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="agent.md"):
        rc.run_experiment(args(tmp_path, model="claude-opus-5-5"))


def test_report_only_never_calls_the_runner(tmp_path, patched, monkeypatch):
    fake_run(tmp_path / "diy" / "rep-1", "diy")
    monkeypatch.setattr(rc, "run_experiment", lambda a: pytest.fail("report-only must not run anything"))
    monkeypatch.setattr(rc.subprocess, "run", lambda *a, **k: pytest.fail("no subprocess in report-only"))
    assert rc.main(["--report-only", "--out", str(tmp_path)]) == 0


# --- the report -------------------------------------------------------------------

def test_report_aggregates_both_implementations_and_keeps_missing_values_missing(tmp_path, patched, capsys):
    fake_run(tmp_path / "diy" / "rep-1", "diy")
    fake_run(tmp_path / "diy" / "rep-2", "diy", status="max_iterations", charts=1)
    fake_run(tmp_path / "managed" / "rep-1", "managed", seconds=300.0)
    rc.write_report(tmp_path, analysis=False, impls=("diy", "managed"))
    data = json.loads((tmp_path / "report.json").read_text())
    diy, managed = data["aggregate"]["diy"], data["aggregate"]["managed"]
    assert (diy["runs"], diy["completed"], diy["required_artifacts_complete"]) == (2, 1, 1)
    assert managed["runs"] == 1 and managed["active_seconds"]["mean"] == 300.0
    assert diy["runtime_cost_usd"]["n"] == 0 and diy["runtime_cost_usd"]["missing"] == 2   # not zero
    assert managed["runtime_cost_usd"]["n"] == 1
    assert diy["analysis_py_runs"] is None                                                   # not checked
    md = (tmp_path / "report.md").read_text()
    assert "unavailable" in md and "| Runs / completed | 2 / 1 | 1 / 1 |" in md
    assert "indicative, not statistically established" in md


def test_report_with_no_runs(tmp_path, patched, capsys):
    rc.write_report(tmp_path, analysis=False, impls=("diy", "managed"))
    assert "no runs" in capsys.readouterr().out and not (tmp_path / "report.md").exists()


def test_spread_ignores_missing_values():
    assert rc.spread([1, None, 3]) == {"mean": 2.0, "min": 1, "max": 3, "n": 2, "missing": 1}
    assert rc.spread([None]) == {"n": 0, "missing": 1}
    assert rc.fmt(rc.spread([None])) == "unavailable"


# --- failure scenarios ---------------------------------------------------------------

def test_failure_runs_need_confirmation_and_report_only_is_free(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "run_main", lambda *a, **k: pytest.fail("no API calls without --yes"))
    with pytest.raises(SystemExit, match="--yes"):
        fs.main(["--out", str(tmp_path)])
    assert fs.main(["--report-only", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "failure-report.md").is_file()


def test_micro_tasks_use_the_bare_environment_and_skip_finished_runs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fs, "run_main", lambda impl, a, timeout_s: calls.append((impl, a)) or 0)
    fs.run_micro("tool_error", "diy", tmp_path)
    assert "--bare" in calls[0][1] and (tmp_path / "tool_error" / "task.md").read_text().startswith("Run the shell command")
    fake_run(tmp_path / "tool_error" / "managed", "managed")
    fs.run_micro("tool_error", "managed", tmp_path)
    assert len(calls) == 1


def test_iteration_limit_is_not_applicable_to_managed(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "run_main", lambda *a, **k: pytest.fail("nothing to run"))
    fs.run_limit("iteration_limit", "managed", tmp_path)
    report = fs.collect(tmp_path, ("diy", "managed"))
    assert "no iteration limit" in report["iteration_limit"]["managed"]["not_applicable"]


def test_facts_and_report_rendering(tmp_path):
    run = fake_run(tmp_path / "syntax_error" / "diy", "diy")
    (run / "outputs" / "result.txt").write_text("OK\n")
    (run / "interruption.json").write_text(json.dumps({"killed_after_tool_calls": 6}))
    report = fs.collect(tmp_path, ("diy", "managed"))
    got = report["syntax_error"]["diy"]
    assert got["result_txt"] == "OK" and got["expected_result"] == "OK" and got["tool_calls_with_error_result"] == 1
    assert "managed" not in report["syntax_error"] or report["syntax_error"]["managed"] == {}
    md = fs.render(report, ("diy", "managed"))
    assert "## syntax_error" in md and "not run" in md


def test_every_scenario_in_the_brief_is_covered():
    # brief section 9.4: syntax error, missing file, timeout, tool error, interrupted client, limits, multi-round
    assert set(fs.MICRO_TASKS) == {"syntax_error", "missing_file", "tool_timeout", "tool_error"}
    assert "client_interrupted" in fs.__doc__ and "budget_limit" in fs.__doc__ and "multiple-rounds" in fs.__doc__
