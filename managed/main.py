"""Run the Managed Agents implementation.

    uv run python -m managed.main --task task/instructions.md             # paid: runs a session
    uv run python -m managed.main --task task/instructions.md --dry-run   # creates resources, runs nothing
    uv run python -m managed.main --attach results/managed/<run>          # re-attach after an interruption

There is no agent loop and no sandbox here. This file configures the agent and
environment, uploads the CSV, starts a session, watches its events, downloads the
outputs and records usage. Everything else runs on Anthropic's side.

Each run gets a directory under results/managed/:
  outputs/       files the agent wrote to /mnt/session/outputs
  session.json   ids needed to re-attach      trace.jsonl  every event, redacted
  run.json       final status and metrics     summary.md   human-readable summary
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".state.json"
INPUT_PATH = "/mnt/session/uploads"
OUTPUT_PATH = "/mnt/session/outputs"
RUNTIME_USD_PER_HOUR = 0.08
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg"}
# USD per million tokens: input, output, 5-minute cache write, cache read.
PRICES = {"claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10), "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20)}
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
RETRYABLE = (anthropic.APIConnectionError, anthropic.InternalServerError, anthropic.RateLimitError)

ENVIRONMENT = f"""
## Environment

- The input CSV is at `{INPUT_PATH}/sales_transactions.csv` (read-only).
- Write `report.md`, `analysis.py`, `charts/` and `summary.json` to `{OUTPUT_PATH}/`. Files outside that directory are not returned to me.
- Python 3 with pandas, numpy and matplotlib is available. There is no network access.
"""


ENVIRONMENT_BARE = f"""
## Environment

- Input files are in `{INPUT_PATH}/` (read-only).
- Your output directory is `{OUTPUT_PATH}/`.
"""


def redact(text: str) -> str:
    text = SECRET.sub("[REDACTED]", text)
    for name, value in os.environ.items():
        if len(value) >= 12 and any(h in name.upper() for h in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[REDACTED]")
    return text


def digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


# --- agent and environment: created once, reused --------------------------------

def read_agent_file(path: Path) -> dict:
    _, front, system = path.read_text(encoding="utf-8").split("---", 2)
    return {**yaml.safe_load(front), "system": system.strip()}


def ensure_resources(client) -> dict:
    """Create the agent and environment on first use; later runs reuse them. Edits to agent.md make a new agent version."""
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.is_file() else {}
    agent_def = read_agent_file(HERE / "agent.md")
    env_def = yaml.safe_load((HERE / "environment.yaml").read_text(encoding="utf-8"))

    agent_hash = digest(json.dumps(agent_def, sort_keys=True))
    if "agent" not in state:
        agent = client.beta.agents.create(**agent_def)
        state["agent"] = {"id": agent.id, "version": agent.version, "hash": agent_hash}
    elif state["agent"]["hash"] != agent_hash:
        agent = client.beta.agents.update(state["agent"]["id"], version=state["agent"]["version"], **agent_def)
        state["agent"] = {"id": agent.id, "version": agent.version, "hash": agent_hash}

    env_hash = digest(json.dumps(env_def, sort_keys=True))
    if "environment" not in state:
        env = client.beta.environments.create(**env_def)
        state["environment"] = {"id": env.id, "hash": env_hash}
    elif state["environment"]["hash"] != env_hash:
        raise SystemExit("environment.yaml changed but environments are not versioned: delete managed/.state.json "
                         "(and the old environment) to create a new one")
    STATE_FILE.write_text(json.dumps(state, indent=2))
    return state


# --- watching a session -------------------------------------------------------

def watch(client, session_id: str, log=print):
    """Yield each session event once, until the caller stops. Reconnects when the stream drops.

    The event stream has no replay, so after every (re)connect we list the stored events and
    de-duplicate by id; otherwise an event that arrived during the gap would be lost.
    """
    seen, failures = set(), 0
    while True:
        try:
            with client.beta.sessions.events.stream(session_id) as stream:
                caught_up = client.beta.sessions.events.list(session_id, order="asc")
                for event in list(caught_up) + [None]:
                    for e in (stream if event is None else [event]):
                        if e.id not in seen:
                            seen.add(e.id)
                            yield e
            failures = 0
        except RETRYABLE as exc:
            failures += 1
            if failures > 5:
                raise
            log(f"    stream dropped ({type(exc).__name__}); reconnecting")
            time.sleep(min(2 ** failures, 30))


class RunState:
    """What we learn from the events: counts, errors, the final stop reason, usage."""

    def __init__(self):
        self.tool_calls = self.model_calls = 0
        self.errors, self.final_text, self.stop = [], "", None
        self.model_usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0}

    def handle(self, e, log) -> bool:
        """Update from one event. Returns True when the session has settled."""
        if e.type == "agent.tool_use":
            self.tool_calls += 1
            log(f"[{self.tool_calls}] {e.name}")
        elif e.type == "agent.message":
            self.final_text = "".join(b.text for b in e.content if b.type == "text") or self.final_text
        elif e.type == "span.model_request_end":
            self.model_calls += 1
            for k in self.model_usage:
                self.model_usage[k] += getattr(e.model_usage, k, 0) or 0
        elif e.type == "session.error":
            self.errors.append(redact(json.dumps(e.model_dump(mode="json"), default=str)))
            log(f"    session error: {self.errors[-1][:200]}")
        elif e.type == "session.status_idle":
            self.stop = e.stop_reason.type
            return self.stop != "requires_action"  # requires_action: waiting on us, keep watching
        elif e.type == "session.status_terminated":
            self.stop = "terminated"
            return True
        return False


def observe(client, session_id: str, trace: Path, deadline: float, log=print) -> RunState:
    state = RunState()
    with trace.open("a", encoding="utf-8") as out:
        for e in watch(client, session_id, log):
            out.write(redact(json.dumps(e.model_dump(mode="json"), default=str)) + "\n")
            if state.handle(e, log):
                break
            if time.monotonic() > deadline:
                client.beta.sessions.events.send(session_id, events=[{"type": "user.interrupt"}])
                state.stop = "timeout"
                break
    return state


# --- outputs and metrics ------------------------------------------------------

def download_outputs(client, session_id: str, dest: Path, log=print) -> list[str]:
    """Fetch files the agent wrote to /mnt/session/outputs. Indexing lags the idle event by a few seconds."""
    files, previous = [], -1
    for _ in range(8):
        files = list(client.beta.files.list(scope_id=session_id, betas=["managed-agents-2026-04-01"]))
        if files and len(files) == previous:
            break
        previous = len(files)
        time.sleep(3)
    saved = []
    for f in files:
        if not getattr(f, "downloadable", True):  # the listing also contains the input file we uploaded
            continue
        # The listing returns bare file names: a file written to outputs/charts/a.png comes back as "a.png".
        # Put images back under charts/ so the output contract holds.
        name = Path(f.filename.removeprefix(OUTPUT_PATH + "/").lstrip("/"))
        rel = Path("charts") / name.name if name.suffix.lower() in IMAGE_SUFFIXES else name
        if rel.is_absolute() or ".." in rel.parts:
            log(f"    skipping unsafe output name {f.filename!r}")
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        client.beta.files.download(f.id).write_to_file(dest / rel)
        saved.append(str(rel))
    return saved


def money(usage_cost) -> str | None:
    return f"{int(usage_cost.amount) / 100:.2f}" if usage_cost is not None else None


def metrics(session, run: RunState, model: str) -> dict:
    u = session.usage
    inp, out, write, read = PRICES[model]
    cache = getattr(u, "cache_creation", None)  # cache writes come back split by lifetime
    tokens = {"input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None),
              "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
              "cache_write_5m_tokens": getattr(cache, "ephemeral_5m_input_tokens", None),
              "cache_write_1h_tokens": getattr(cache, "ephemeral_1h_input_tokens", None)}
    active = getattr(u, "active_seconds", None)
    token_cost = None
    if all(v is not None for v in tokens.values()):
        token_cost = (tokens["input_tokens"] * inp + tokens["output_tokens"] * out
                      + tokens["cache_write_5m_tokens"] * write + tokens["cache_write_1h_tokens"] * 2 * inp
                      + tokens["cache_read_input_tokens"] * read) / 1e6
    return {"session_usage": tokens, "active_seconds": active,
            "events_usage": run.model_usage, "model_calls": run.model_calls, "tool_calls": run.tool_calls,
            "estimated_token_cost_usd": None if token_cost is None else round(token_cost, 4),
            "estimated_runtime_cost_usd": None if active is None else round(active / 3600 * RUNTIME_USD_PER_HOUR, 4),
            "list_cost_usd_reported": money(getattr(u, "list_cost", None))}


def finish(client, session_id: str, run_dir: Path, run: RunState, model: str, started: float, log=print,
           input_file_id: str | None = None) -> int:
    session = client.beta.sessions.retrieve(session_id)
    saved = download_outputs(client, session_id, run_dir / "outputs", log)
    if input_file_id:  # the uploaded input is ours to clean up
        try:
            client.beta.files.delete(input_file_id)
        except anthropic.APIError as e:
            log(f"    could not delete the uploaded input file: {e}")
    status = {"end_turn": "completed", "budget_reached": "budget_exhausted", "timeout": "max_duration"}.get(
        run.stop, "failed")
    if run.errors and status == "completed":
        status = "completed_with_errors"
    summary = {"implementation": "managed", "session_id": session_id, "model": model, "status": status,
               "stop_reason": run.stop, "session_status": session.status, "errors": run.errors,
               "wall_seconds": round(time.monotonic() - started, 1), "artifacts": saved,
               "metrics": metrics(session, run, model), "final_message": run.final_text}
    (run_dir / "run.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(render_summary(summary), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "final_message"}, indent=2))
    print(f"\nrun directory: {run_dir}")
    return 0 if status == "completed" else 1


def render_summary(s: dict) -> str:
    m = s["metrics"]
    lines = [f"# Managed run {s['session_id']}", "", f"- Status: **{s['status']}** (stop reason {s['stop_reason']}, "
             f"session {s['session_status']})", f"- Model: {s['model']}",
             f"- Active session time: {m['active_seconds']}s",
             f"- Model calls: {m['model_calls']}   Tool calls: {m['tool_calls']}",
             f"- Tokens (session): {m['session_usage']}",
             f"- Estimated cost: tokens ${m['estimated_token_cost_usd']} + runtime ${m['estimated_runtime_cost_usd']}; "
             f"reported list cost ${m['list_cost_usd_reported']}", "", "## Artifacts", ""]
    lines += [f"- `{a}`" for a in s["artifacts"]] or ["- none"]
    if s["errors"]:
        lines += ["", "## Errors", ""] + [f"- {e[:300]}" for e in s["errors"]]
    return "\n".join(lines) + "\n"


# --- entry point ----------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", type=Path)
    ap.add_argument("--attach", type=Path, metavar="RUN_DIR", help="re-attach to a session started earlier")
    ap.add_argument("--data", type=Path, default=ROOT / "task" / "data" / "sales_transactions.csv")
    ap.add_argument("--max-cost", type=float, default=3.00, help="USD session budget (list price, includes runtime)")
    ap.add_argument("--timeout-minutes", type=float, default=30)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="create the resources and a session, but run no work")
    ap.add_argument("--bare", action="store_true", help="describe only the input and output directories (fault-injection tasks)")
    a = ap.parse_args(argv)
    if not a.task and not a.attach:
        ap.error("--task is required unless --attach is given")

    load_dotenv(ROOT / ".env.local")
    load_dotenv(ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set (put it in .env.local)", file=sys.stderr)
        return 2
    client = anthropic.Anthropic()
    started = time.monotonic()
    deadline = started + a.timeout_minutes * 60

    if a.attach:
        info = json.loads((a.attach / "session.json").read_text())
        run = observe(client, info["session_id"], a.attach / "trace.jsonl", deadline)
        return finish(client, info["session_id"], a.attach, run, info["model"], started, input_file_id=info.get("file_id"))

    state = ensure_resources(client)
    model = read_agent_file(HERE / "agent.md")["model"]
    run_dir = a.out or ROOT / "results" / "managed" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    with a.data.open("rb") as f:
        csv = client.beta.files.upload(file=(a.data.name, f, "text/csv"))
    task = a.task.read_text(encoding="utf-8").rstrip() + (ENVIRONMENT_BARE if a.bare else ENVIRONMENT) if a.task else ""
    session = client.beta.sessions.create(
        agent={"type": "agent", "id": state["agent"]["id"], "version": state["agent"]["version"]},
        environment_id=state["environment"]["id"], title=f"diy-vs-managed {run_dir.name}",
        resources=[{"type": "file", "file_id": csv.id, "mount_path": f"/{a.data.name}"}],
        budget={"type": "limit", "max_list_cost": {"amount": str(int(a.max_cost * 100)), "currency": "USD"}},
        initial_events=[] if a.dry_run else [{"type": "user.message", "content": [{"type": "text", "text": task}]}])
    (run_dir / "session.json").write_text(json.dumps(
        {"session_id": session.id, "model": model, "file_id": csv.id, **state}, indent=2))
    print(f"session {session.id}\nConsole: https://platform.claude.com/workspaces/default/sessions/{session.id}")
    if a.dry_run:
        print(f"dry run: agent {state['agent']['id']} v{state['agent']['version']}, environment "
              f"{state['environment']['id']}, file mounted; no work was started")
        client.beta.sessions.delete(session.id)
        client.beta.files.delete(csv.id)
        return 0
    run = observe(client, session.id, run_dir / "trace.jsonl", deadline)
    return finish(client, session.id, run_dir, run, model, started, input_file_id=csv.id)


if __name__ == "__main__":
    raise SystemExit(main())
