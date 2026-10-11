"""Run both implementations repeatedly on the same task, then score and summarise the results.

    uv run python -m experiments.run_comparison --report-only                # no API calls: rebuild the report
    uv run python -m experiments.run_comparison --repetitions 5 --yes        # paid: 5 runs of each implementation

Paid runs need --yes. The runner prints an estimate first and stops launching runs when the
experiment budget would be exceeded. Runs that already have a run.json are skipped, so an
interrupted experiment resumes where it stopped. Runs alternate which implementation goes first.

Layout: <out>/<implementation>/rep-N/{run.json, outputs/, ...}; the report goes to <out>/report.{md,json}.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from statistics import mean

import pandas as pd
from jsonschema import Draft202012Validator

from evaluation.evaluate import DATASET_PATH, SCHEMA_PATH, evaluate_run
from evaluation.ground_truth import DEFAULT_PATH, load_ground_truth
from evaluation.metrics import load_run_metrics
from evaluation.reproduce import docker_ready

ROOT = Path(__file__).resolve().parent.parent
IMPLEMENTATIONS = ("diy", "managed")
# Mean cost of the pilot runs (one or two per side, before the experiment); only used for the pre-run estimate.
PILOT_COST_USD = {"diy": 0.36, "managed": 0.60}


# --- running ------------------------------------------------------------------------

def managed_model() -> str:
    from managed.main import HERE, read_agent_file
    return read_agent_file(HERE / "agent.md")["model"]


def launch(impl: str, run_dir: Path, a) -> int:
    cmd = [sys.executable, "-m", f"{impl}.main", "--task", str(ROOT / "task" / "instructions.md"),
           "--out", str(run_dir), "--max-cost", str(a.max_cost_per_run), "--timeout-minutes", str(a.timeout_minutes)]
    if impl == "diy":
        cmd += ["--model", a.model, "--max-iterations", str(a.max_iterations)]
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    with (run_dir.parent / f"{run_dir.name}.log").open("w", encoding="utf-8") as log:
        return subprocess.run(cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT).returncode


def run_experiment(a) -> None:
    if a.model != managed_model() and "managed" in a.implementations:
        raise SystemExit(f"--model {a.model} differs from managed/agent.md ({managed_model()}). Edit agent.md, or the "
                         "comparison is not like for like.")
    plan = [(rep, impl) for rep in range(1, a.repetitions + 1)
            for impl in (a.implementations if rep % 2 else tuple(reversed(a.implementations)))]
    todo = [(r, i) for r, i in plan if not (a.out / i / f"rep-{r}" / "run.json").exists()]
    estimate = sum(PILOT_COST_USD[i] for _, i in todo)
    print(f"{len(todo)} runs to do ({len(plan) - len(todo)} already done). Estimated cost ${estimate:.2f} "
          f"(from pilot runs), hard stop at ${a.max_experiment_cost:.2f}; per-run cap ${a.max_cost_per_run:.2f}.")
    if not a.yes:
        raise SystemExit("this would spend money: re-run with --yes to proceed")

    spent = sum(m["total_cost_usd"] or 0 for m in (load_run_metrics(p.parent) for p in a.out.glob("*/rep-*/run.json")))  # unknown costs count as 0 here, which only matters for runs from before this experiment
    for rep, impl in todo:
        if spent + PILOT_COST_USD[impl] > a.max_experiment_cost:
            print(f"stopping: ${spent:.2f} spent, the next run could exceed the ${a.max_experiment_cost:.2f} budget")
            break
        run_dir = a.out / impl / f"rep-{rep}"
        start = time.monotonic()
        print(f"[{impl} rep {rep}] starting", flush=True)
        code = launch(impl, run_dir, a)
        cost = load_run_metrics(run_dir)["total_cost_usd"] if (run_dir / "run.json").exists() else None
        spent += a.max_cost_per_run if cost is None else cost  # no record of the cost: assume the worst
        print(f"[{impl} rep {rep}] exit {code} after {time.monotonic() - start:.0f}s; ${spent:.2f} spent so far", flush=True)


# --- reporting ------------------------------------------------------------------------

def spread(values: list) -> dict:
    known = [v for v in values if v is not None]
    return {"mean": round(mean(known), 3), "min": min(known), "max": max(known), "n": len(known),
            "missing": len(values) - len(known)} if known else {"n": 0, "missing": len(values)}


def collect(out: Path, analysis: bool) -> list[dict]:
    truth = load_ground_truth(DEFAULT_PATH)
    df = pd.read_csv(DATASET_PATH)
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    runs = []
    for run_json in sorted(out.glob("*/rep-*/run.json")):
        run = run_json.parent
        metrics = load_run_metrics(run)
        evaluation = evaluate_run(run / "outputs", truth, df, validator, dataset=DATASET_PATH, reproduce=analysis)
        runs.append({"metrics": metrics, "evaluation": evaluation})
    return runs


def aggregate(runs: list[dict], impl: str) -> dict:
    mine = [r for r in runs if r["metrics"]["implementation"] == impl]
    det = [r["evaluation"].get("detection") for r in mine]
    claims = [r["evaluation"].get("numeric_claims") for r in mine]
    repro = [r["evaluation"].get("analysis_reproduction") for r in mine]
    m = lambda key: [r["metrics"][key] for r in mine]
    return {
        "runs": len(mine),
        "completed": sum(r["metrics"]["completed"] for r in mine),
        "required_artifacts_complete": sum(r["evaluation"]["required_artifacts_complete"] for r in mine),
        "schema_valid": sum(r["evaluation"]["schema_valid"] for r in mine),
        "analysis_py_runs": None if not any(repro) else sum(bool(x and x.get("ok")) for x in repro),
        "injected_recall": spread([d["injected_recall"] if d else None for d in det]),
        "findings": spread([r["evaluation"]["finding_count"] for r in mine]),
        "unmatched_findings": spread([len(d["unmatched_findings"]) if d else None for d in det]),
        "legit_flagged_as_problems": spread([len(d["legit_patterns_flagged_as_problems"]) if d else None for d in det]),
        "claims_supported": spread([c["supported"] if c and c.get("available") else None for c in claims]),
        "claims_unsupported": spread([c["unsupported"] if c and c.get("available") else None for c in claims]),
        "claims_unverifiable": spread([c["unverifiable"] if c and c.get("available") else None for c in claims]),
        "active_seconds": spread(m("active_seconds")), "model_calls": spread(m("model_calls")),
        "tool_calls": spread(m("tool_calls")), "input_tokens": spread(m("input_tokens")),
        "output_tokens": spread(m("output_tokens")), "cache_read_tokens": spread(m("cache_read_tokens")),
        "cache_write_tokens": spread(m("cache_write_tokens")),
        "token_cost_usd": spread(m("token_cost_usd")), "runtime_cost_usd": spread(m("runtime_cost_usd")),
        "total_cost_usd": spread(m("total_cost_usd")), "infrastructure_errors": spread(m("infrastructure_errors")),
        "detected_by_anomaly": {aid: sum(aid in d["detected"] for d in det if d)
                                for aid in sorted({x for d in det if d for x in d["detected"] + d["missed"]})},
    }


def fmt(s: dict, digits: int = 1) -> str:
    if not s.get("n"):
        return "unavailable"
    text = f"{s['mean']:.{digits}f} ({s['min']:.{digits}f}-{s['max']:.{digits}f})"
    return text + (f" [{s['missing']} missing]" if s["missing"] else "")


def render(runs: list[dict], agg: dict, impls: tuple) -> str:
    rows = [("Runs / completed", lambda g: f"{g['runs']} / {g['completed']}"),
            ("Required artifacts complete", lambda g: f"{g['required_artifacts_complete']} of {g['runs']}"),
            ("analysis.py runs on the CSV", lambda g: "not checked" if g["analysis_py_runs"] is None
             else f"{g['analysis_py_runs']} of {g['runs']}"),
            ("Injected anomalies found (recall)", lambda g: fmt(g["injected_recall"], 2)),
            ("Findings reported", lambda g: fmt(g["findings"])),
            ("Findings matching nothing in the key", lambda g: fmt(g["unmatched_findings"])),
            ("Legitimate patterns flagged as problems", lambda g: fmt(g["legit_flagged_as_problems"])),
            ("Numeric claims supported by recomputation", lambda g: fmt(g["claims_supported"])),
            ("Numeric claims unsupported (not necessarily wrong)", lambda g: fmt(g["claims_unsupported"])),
            ("Numeric claims unverifiable", lambda g: fmt(g["claims_unverifiable"])),
            ("Active time (s)", lambda g: fmt(g["active_seconds"], 0)),
            ("Model calls", lambda g: fmt(g["model_calls"])), ("Tool calls", lambda g: fmt(g["tool_calls"])),
            ("Input tokens, uncached", lambda g: fmt(g["input_tokens"], 0)),
            ("Output tokens", lambda g: fmt(g["output_tokens"], 0)),
            ("Cache read tokens", lambda g: fmt(g["cache_read_tokens"], 0)),
            ("Cache write tokens", lambda g: fmt(g["cache_write_tokens"], 0)),
            ("Token cost (USD)", lambda g: fmt(g["token_cost_usd"], 3)),
            ("Platform runtime charge (USD)", lambda g: fmt(g["runtime_cost_usd"], 4)),
            ("Total cost (USD)", lambda g: fmt(g["total_cost_usd"], 3)),
            ("Infrastructure errors recorded", lambda g: fmt(g["infrastructure_errors"]))]
    lines = ["# Experiment results", "", "Each cell is mean (min-max) over the runs; `unavailable` means the value was not observable, "
             "not zero. With this few runs, treat differences as indicative, not statistically established.", "",
             "| Measure | " + " | ".join(impls) + " |", "|---|" + "---|" * len(impls)]
    lines += [f"| {name} | " + " | ".join(fn(agg[i]) for i in impls) + " |" for name, fn in rows]
    ids = sorted({a for i in impls for a in agg[i]["detected_by_anomaly"]})
    lines += ["", "## Anomalies detected (runs that found each one)", "", "| Entry | " + " | ".join(impls) + " |",
              "|---|" + "---|" * len(impls)]
    lines += [f"| {aid} | " + " | ".join(f"{agg[i]['detected_by_anomaly'].get(aid, 0)} of {agg[i]['runs']}" for i in impls) + " |"
              for aid in ids]
    lines += ["", "## Per run", "", "| Implementation | Run | Status | Recall | Unmatched | Tool calls | Active s | Cost USD |",
              "|---|---|---|---|---|---|---|---|"]
    for r in runs:
        m, e = r["metrics"], r["evaluation"]
        d = e.get("detection")
        lines.append(f"| {m['implementation']} | {m['run']} | {m['status']} | "
                     f"{'n/a' if not d else format(d['injected_recall'], '.0%')} | {'n/a' if not d else len(d['unmatched_findings'])} | "
                     f"{m['tool_calls']} | {m['active_seconds'] and round(m['active_seconds'])} | {m['total_cost_usd']} |")
    return "\n".join(lines) + "\n"


def write_report(out: Path, analysis: bool, impls: tuple) -> None:
    runs = collect(out, analysis)
    if not runs:
        print(f"no runs under {out}")
        return
    present = tuple(i for i in impls if any(r["metrics"]["implementation"] == i for r in runs))
    agg = {i: aggregate(runs, i) for i in present}
    (out / "report.json").write_text(json.dumps({"aggregate": agg, "runs": runs}, indent=2, default=str) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render(runs, agg, present), encoding="utf-8")
    print((out / "report.md").read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report-only", action="store_true", help="make no API calls; rebuild the report from existing runs")
    ap.add_argument("--yes", action="store_true", help="confirm that this may spend money")
    ap.add_argument("--repetitions", type=int, default=5)
    ap.add_argument("--implementations", nargs="+", default=list(IMPLEMENTATIONS), choices=IMPLEMENTATIONS)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--timeout-minutes", type=float, default=30)
    ap.add_argument("--max-iterations", type=int, default=40, help="DIY only; Managed Agents has no iteration limit")
    ap.add_argument("--max-cost-per-run", type=float, default=3.00)
    ap.add_argument("--max-experiment-cost", type=float, default=8.00)
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "experiment-1")
    ap.add_argument("--no-analysis-run", action="store_true", help="skip re-running each analysis.py in a container")
    a = ap.parse_args(argv)
    a.implementations = tuple(a.implementations)

    if not a.report_only:
        run_experiment(a)
    write_report(a.out, analysis=not a.no_analysis_run and docker_ready(), impls=a.implementations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
