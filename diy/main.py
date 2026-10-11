"""Run the DIY agent.

    uv run python -m diy.main --task task/instructions.md             # paid: calls the API
    uv run python -m diy.main --task task/instructions.md --dry-run   # no model calls; checks the sandbox
    uv run python -m diy.main --resume results/diy/<run>              # continue an interrupted run

Each run gets a fresh container and its own directory under results/diy/:
  outputs/        the agent's workspace (report.md, analysis.py, charts/, summary.json)
  session.sqlite  conversation, tool calls, usage, errors
  trace.jsonl     structured event trace
  run.json        final status and metrics        summary.md  human-readable summary
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from diy.agent_loop import PRICES, Tracer, cost_usd, run_agent
from diy.sandbox import DATA_MOUNT, WORKSPACE, Sandbox, SandboxError
from diy.sessions import RESUMABLE, Limits, Session
from diy.tools import Dispatcher

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "claude-sonnet-5-5"

ENVIRONMENT = """
## Environment

- The input CSV is at `{data}` (read-only).
- Your working directory and output directory is `{workspace}`. Write `report.md`, `analysis.py`, `charts/` and `summary.json` there.
- Python 3 with pandas, numpy and matplotlib is available. There is no network access.
"""


def build_task(task_path: Path, data_name: str) -> str:
    env = ENVIRONMENT.format(data=f"{DATA_MOUNT}/{data_name}", workspace=WORKSPACE)
    return task_path.read_text(encoding="utf-8").rstrip() + "\n" + env


def dry_run(dispatcher: Dispatcher) -> int:
    """Exercise the sandbox and tools with no model calls."""
    steps = [("execute_python", {"code": "import pandas as pd, glob\nf = glob.glob('/data/*.csv')[0]\n"
                                         "print(pd.read_csv(f).shape)"}),
             ("write_file", {"path": "dry_run.txt", "content": "ok"}),
             ("list_files", {})]
    for name, args in steps:
        text, is_error = dispatcher.run(name, args)
        print(f"{name}: {'ERROR' if is_error else 'ok'}\n{text}\n")
        if is_error:
            return 1
    return 0


def write_outputs(session: Session, run_dir: Path, result) -> None:
    summary = {"implementation": "diy", "session_id": session.id, "model": session.model, "status": result.status,
               "error": result.error, "iterations": result.iterations, "active_seconds": round(result.duration_s, 1),
               "estimated_cost_usd": round(result.cost_usd, 4), "usage": vars(result.usage),
               "final_message": result.final_text}
    (run_dir / "run.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(session.render_markdown(run_dir / "outputs", result.cost_usd), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "final_message"}, indent=2))
    print(f"\nrun directory: {run_dir}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", type=Path)
    ap.add_argument("--resume", type=Path, metavar="RUN_DIR", help="continue the session in this run directory")
    ap.add_argument("--system", type=Path, default=ROOT / "task" / "system.md")
    ap.add_argument("--data", type=Path, default=ROOT / "task" / "data" / "sales_transactions.csv")
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(PRICES))
    ap.add_argument("--max-iterations", type=int)
    ap.add_argument("--timeout-minutes", type=float)
    ap.add_argument("--max-cost", type=float, help="USD; the run stops when estimated cost reaches this")
    ap.add_argument("--out", type=Path, default=None, help="run directory (default results/diy/<timestamp>)")
    ap.add_argument("--dry-run", action="store_true", help="start the sandbox and run the tools; make no model calls")
    a = ap.parse_args(argv)
    if not a.task and not a.resume:
        ap.error("--task is required unless --resume is given")

    load_dotenv(ROOT / ".env.local")
    load_dotenv(ROOT / ".env")
    if not a.data.is_file():
        print(f"dataset not found: {a.data}. Run task/generate_data.py first.", file=sys.stderr)
        return 2
    if not a.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (put it in .env.local)", file=sys.stderr)
        return 2

    if a.resume:
        run_dir = a.resume
        session = Session.load(run_dir / "session.sqlite")
        if session.status not in RESUMABLE:
            print(f"session is {session.status!r}; only {sorted(RESUMABLE)} can be resumed", file=sys.stderr)
            return 2
        cur = session.limits  # only the limits you pass again are changed
        session.set_limits(Limits(a.max_iterations or cur.max_iterations,
                                  int(a.timeout_minutes * 60) if a.timeout_minutes else cur.max_seconds,
                                  a.max_cost or cur.max_cost_usd))
    else:
        run_dir = a.out or ROOT / "results" / "diy" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        limits = Limits(a.max_iterations or 40, int((a.timeout_minutes or 30) * 60), a.max_cost or 3.00)
        session = None if a.dry_run else Session.create(
            run_dir / "session.sqlite", task=build_task(a.task, a.data.name),
            system=a.system.read_text(encoding="utf-8").strip(), model=a.model, limits=limits)

    try:
        with Sandbox(a.data, run_dir / "outputs") as sandbox:  # a fresh container every time, even on resume
            dispatcher = Dispatcher(sandbox, python_calls=session.count_tool_calls("execute_python") if session else 0)
            if a.dry_run:
                return dry_run(dispatcher)
            result = run_agent(anthropic.Anthropic(), session=session, dispatcher=dispatcher,
                               tracer=Tracer(run_dir / "trace.jsonl"))
    except SandboxError as e:  # could not even start the sandbox
        if session:
            session.add_error("sandbox", str(e))
            session.set_status("sandbox_error", str(e))
        print(f"sandbox failure: {e}", file=sys.stderr)
        return 3

    write_outputs(session, run_dir, result)
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
