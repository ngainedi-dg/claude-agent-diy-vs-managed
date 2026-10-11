"""Export a metrics-only summary of the results, safe to commit.

    uv run python -m experiments.export_results

Reads the local, gitignored results (results/experiment-1/report.json and results/failures/failure-report.json)
and writes docs/results/. Fields are copied from an explicit allowlist: numbers, booleans, statuses and
anomaly IDs only. Nothing the agents wrote (reports, findings, final messages, tool inputs or outputs),
no session, agent or file IDs, and no timestamps are exported.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RUN_METRIC_FIELDS = ("implementation", "run", "model", "status", "completed", "active_seconds", "model_calls", "tool_calls",
                     "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "peak_context_tokens",
                     "infrastructure_errors", "token_cost_usd", "runtime_cost_usd", "total_cost_usd",
                     "reported_list_cost_usd", "artifact_files")
DETECTION_FIELDS = ("injected_total", "injected_detected", "injected_recall", "detected", "missed", "ambiguous_detected",
                    "legit_patterns_mentioned")
CLAIM_FIELDS = ("supported", "unsupported", "unverifiable", "checked_fraction")
SCENARIO_FIELDS = ("status", "result_txt", "expected_result", "model_calls", "tool_calls", "tool_calls_with_error_result",
                   "infrastructure_errors", "active_seconds", "cost_usd")


def pick(source: dict | None, fields) -> dict:
    return {f: source[f] for f in fields if source and f in source}


def export_experiment(report: dict) -> dict:
    runs = []
    for r in report["runs"]:
        e = r["evaluation"]
        det, claims, repro = e.get("detection"), e.get("numeric_claims"), e.get("analysis_reproduction")
        runs.append({
            **pick(r["metrics"], RUN_METRIC_FIELDS),
            "schema_valid": e["schema_valid"],
            "required_artifacts_complete": e["required_artifacts_complete"],
            "finding_count": e["finding_count"],
            "chart_count": e["artifacts"]["chart_count"],
            "analysis_py_runs": None if not repro else bool(repro.get("ok")),
            "detection": None if not det else {
                **pick(det, DETECTION_FIELDS),
                "unmatched_findings": len(det["unmatched_findings"]),
                "legit_patterns_flagged_as_problems": len(det["legit_patterns_flagged_as_problems"])},
            "numeric_claims": pick(claims, CLAIM_FIELDS) if claims and claims.get("available") else None,
        })
    return {"runs": runs, "aggregate": report["aggregate"]}


def export_scenarios(report: dict) -> dict:
    out = {}
    for name, per in report.items():
        out[name] = {}
        for impl, facts in per.items():
            if "not_applicable" in facts:
                out[name][impl] = {"not_applicable": True}
                continue
            item = pick(facts, SCENARIO_FIELDS)
            if "interruption" in facts:
                item["killed_after_tool_calls"] = facts["interruption"].get("killed_after_tool_calls")
            out[name][impl] = item
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--experiment", type=Path, default=ROOT / "results" / "experiment-1")
    ap.add_argument("--failures", type=Path, default=ROOT / "results" / "failures")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "results")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)

    report = json.loads((a.experiment / "report.json").read_text(encoding="utf-8"))
    (a.out / "experiment-1.json").write_text(json.dumps(export_experiment(report), indent=2) + "\n", encoding="utf-8")
    (a.out / "experiment-1.md").write_text((a.experiment / "report.md").read_text(encoding="utf-8"), encoding="utf-8")
    failures = json.loads((a.failures / "failure-report.json").read_text(encoding="utf-8"))
    (a.out / "failure-scenarios.json").write_text(json.dumps(export_scenarios(failures), indent=2) + "\n", encoding="utf-8")
    (a.out / "failure-scenarios.md").write_text((a.failures / "failure-report.md").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
