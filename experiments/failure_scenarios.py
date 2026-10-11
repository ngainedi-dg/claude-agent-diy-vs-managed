"""Reliability scenarios: inject the same fault into both implementations and record what happened.

    uv run python -m experiments.failure_scenarios --report-only         # no API calls
    uv run python -m experiments.failure_scenarios --yes                  # paid: runs every scenario

The faults are applied through the task (short micro-tasks with a known right answer), because a
hosted runtime cannot have faults injected into it from outside. Where the two environments cannot
be given an identical fault, the report says so. The runner records facts (status, answer, cost);
who handled each failure (model, application or runtime) is a judgement made from the traces and
written up in docs/comparison.md.

Scenarios: syntax_error, missing_file, tool_timeout, tool_error, budget_limit, iteration_limit,
client_interrupted. The multiple-rounds scenario is covered by every run of the main experiment.
"""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from evaluation.metrics import load_run_metrics

ROOT = Path(__file__).resolve().parent.parent
IMPLEMENTATIONS = ("diy", "managed")

MICRO_TASKS = {
    "syntax_error": (
        "Run this exact Python code first, then report exactly what happened:\n\n```python\nprint('start'\nx = 1\n```\n\n"
        "Once you have confirmed what the error was, write the single word OK to a file named result.txt in the output "
        "directory, then stop.",
        "OK", "the run completes and result.txt says OK"),
    "missing_file": (
        "Count the rows in the CSV file named sales_2024.csv in your input directory. If you cannot find that file, write "
        "the single word MISSING to a file named result.txt in the output directory and stop. Do not guess or use a "
        "different file.", "MISSING", "result.txt says MISSING, with no invented row count"),
    "tool_timeout": (
        "Run this command exactly once and do not shorten it: `python3 -c \"import time; time.sleep(600)\"`. If the command "
        "does not finish normally, write the single word TIMEOUT to a file named result.txt in the output directory; if "
        "it finishes, write DONE. Then stop.", "TIMEOUT",
        "observed only: the two sandboxes enforce different, undocumented-for-us tool timeouts"),
    "tool_error": (
        "Run the shell command `ls /nonexistent_directory`. Write the exit code of that command as a bare number to a "
        "file named result.txt in the output directory, then stop.", "2", "result.txt says 2"),
}


# --- helpers ----------------------------------------------------------------------------

def run_main(impl: str, args: list[str], timeout_s: int) -> int:
    cmd = [sys.executable, "-m", f"{impl}.main", *args]
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=timeout_s).returncode
    except subprocess.TimeoutExpired:
        return -1


def cleanup_containers() -> None:
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", "name=diy-agent-"], capture_output=True, text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)


def facts(impl: str, run_dir: Path) -> dict:
    """What the run recorded, in the same shape for both implementations."""
    if not (run_dir / "run.json").exists():
        return {"recorded": False}
    info = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    m = load_run_metrics(run_dir)
    result = run_dir / "outputs" / "result.txt"
    out = {"recorded": True, "status": info["status"], "model_calls": m["model_calls"], "tool_calls": m["tool_calls"],
           "active_seconds": m["active_seconds"], "cost_usd": m["total_cost_usd"],
           "infrastructure_errors": m["infrastructure_errors"],
           "result_txt": result.read_text(encoding="utf-8").strip() if result.is_file() else None,
           "final_message": (info.get("final_message") or "")[:400]}
    if impl == "diy" and (run_dir / "session.sqlite").is_file():
        con = sqlite3.connect(run_dir / "session.sqlite")
        out["tool_calls_with_error_result"] = con.execute("SELECT COUNT(*) FROM tool_calls WHERE is_error=1").fetchone()[0]
        con.close()
    return out


def micro_task(out: Path, name: str) -> Path:
    path = out / name / "task.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MICRO_TASKS[name][0] + "\n", encoding="utf-8")
    return path


# --- scenarios --------------------------------------------------------------------------

def run_micro(name: str, impl: str, out: Path) -> None:
    run_dir = out / name / impl
    if (run_dir / "run.json").exists():
        return
    minutes = 20 if name == "tool_timeout" else 10
    run_main(impl, ["--task", str(micro_task(out, name)), "--bare", "--out", str(run_dir), "--max-cost", "0.75",
                    "--timeout-minutes", str(minutes)], timeout_s=minutes * 60 + 120)


def run_limit(name: str, impl: str, out: Path) -> None:
    run_dir = out / name / impl
    if (run_dir / "run.json").exists():
        return
    task = str(ROOT / "task" / "instructions.md")
    if name == "budget_limit":
        extra = ["--max-cost", "0.05"]
    elif impl == "diy":
        extra = ["--max-iterations", "3"]
    else:
        (run_dir).mkdir(parents=True, exist_ok=True)
        (run_dir / "not_applicable.txt").write_text("Managed Agents has no iteration limit to set.\n")
        return
    run_main(impl, ["--task", task, "--out", str(run_dir), *extra], timeout_s=1200)


def progress(impl: str, run_dir: Path) -> int:
    """How many tool calls the run has made so far, read from what the implementation writes as it goes."""
    try:
        if impl == "diy":
            con = sqlite3.connect(run_dir / "session.sqlite")
            n = con.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
            con.close()
            return n
        return (run_dir / "trace.jsonl").read_text(encoding="utf-8").count('"type": "agent.tool_use"')
    except (OSError, sqlite3.Error):
        return 0


def run_interrupted(impl: str, out: Path, kill_after: int = 6) -> None:
    run_dir = out / "client_interrupted" / impl
    if (run_dir / "run.json").exists():
        return
    proc = subprocess.Popen([sys.executable, "-m", f"{impl}.main", "--task", str(ROOT / "task" / "instructions.md"),
                             "--out", str(run_dir), "--max-cost", "3.00"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 900
    while proc.poll() is None and progress(impl, run_dir) < kill_after and time.monotonic() < deadline:
        time.sleep(2)
    killed_after = progress(impl, run_dir)
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)  # no cleanup code gets to run
        proc.wait()
    (run_dir / "interruption.json").write_text(json.dumps({"killed_after_tool_calls": killed_after}))
    if impl == "diy":
        cleanup_containers()  # the orphaned sandbox (see engineering log); a managed session has no local container
    flag = "--resume" if impl == "diy" else "--attach"
    run_main(impl, [flag, str(run_dir), "--max-cost", "3.00"], timeout_s=1800)


# --- report -----------------------------------------------------------------------------

def collect(out: Path, impls: tuple) -> dict:
    report = {}
    for name in [*MICRO_TASKS, "budget_limit", "iteration_limit", "client_interrupted"]:
        report[name] = {}
        for impl in impls:
            run_dir = out / name / impl
            if (run_dir / "not_applicable.txt").exists():
                report[name][impl] = {"not_applicable": run_dir.joinpath("not_applicable.txt").read_text().strip()}
            elif run_dir.exists():
                report[name][impl] = facts(impl, run_dir)
                if (run_dir / "interruption.json").exists():
                    report[name][impl]["interruption"] = json.loads((run_dir / "interruption.json").read_text())
                if name in MICRO_TASKS and report[name][impl].get("recorded"):
                    report[name][impl]["expected_result"] = MICRO_TASKS[name][1]
    return report


def render(report: dict, impls: tuple) -> str:
    lines = ["# Reliability scenarios", "", "Facts recorded by the runner. Who handled each failure is judged from the traces "
             "in docs/comparison.md.", ""]
    for name, per in report.items():
        lines += [f"## {name}", ""]
        if name in MICRO_TASKS:
            lines += [f"Expected: {MICRO_TASKS[name][2]}", ""]
        lines += ["| | " + " | ".join(impls) + " |", "|---|" + "---|" * len(impls)]
        keys = ["status", "result_txt", "model_calls", "tool_calls", "tool_calls_with_error_result", "infrastructure_errors",
                "active_seconds", "cost_usd", "interruption"]
        for k in keys:
            vals = [str(per.get(i, {}).get(k, "-")) if per.get(i, {}).get("recorded") else
                    ("not run" if i not in per else (per[i].get("not_applicable", "no record") if k == "status" else "-"))
                    for i in impls]
            if any(v not in ("-", "None") for v in vals):
                lines.append(f"| {k} | " + " | ".join(vals) + " |")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--yes", action="store_true", help="confirm that this may spend money")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "failures")
    ap.add_argument("--implementations", nargs="+", default=list(IMPLEMENTATIONS), choices=IMPLEMENTATIONS)
    ap.add_argument("--scenarios", nargs="+", help="run only these scenarios")
    a = ap.parse_args(argv)
    impls = tuple(a.implementations)
    names = a.scenarios or [*MICRO_TASKS, "budget_limit", "iteration_limit", "client_interrupted"]

    if not a.report_only:
        if not a.yes:
            raise SystemExit("this would spend money (estimated about $1.50-$2.00 for all scenarios): re-run with --yes")
        for name in names:
            for impl in impls:
                print(f"[{name}] {impl}", flush=True)
                if name in MICRO_TASKS:
                    run_micro(name, impl, a.out)
                elif name in ("budget_limit", "iteration_limit"):
                    run_limit(name, impl, a.out)
                elif name == "client_interrupted":
                    run_interrupted(impl, a.out)
    report = collect(a.out, impls)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "failure-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (a.out / "failure-report.md").write_text(render(report, impls), encoding="utf-8")
    print(render(report, impls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
