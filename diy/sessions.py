"""Session persistence in SQLite: enough to inspect a run and to resume one after an interruption.

One database per run (`session.sqlite`), next to the agent's workspace but not inside it.
Everything written here is redacted first, because traces and sessions may be published.

Recovery rule: a tool call is recorded as `started` before it runs and `done` after. A call
that is still `started` when a session is resumed may or may not have taken effect, so the
loop never runs it again; it tells the model the outcome is unknown.
"""

import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SECRET_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
SECRET_ENV_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
RESUMABLE = {"running", "interrupted", "api_error", "sandbox_error", "max_iterations", "max_duration", "budget_exhausted"}


def redact(text: str) -> str:
    """Remove API-key-shaped strings and the values of secret-looking environment variables."""
    text = SECRET_PATTERN.sub("[REDACTED]", text)
    for name, value in os.environ.items():
        if len(value) >= 12 and any(h in name.upper() for h in SECRET_ENV_HINTS):
            text = text.replace(value, "[REDACTED]")
    return text


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Limits:
    max_iterations: int = 40
    max_seconds: int = 1800
    max_cost_usd: float = 3.00


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    model_calls: int = 0
    peak_context_tokens: int = 0  # largest prompt sent in one call: how close we got to the context window

    def add(self, u) -> None:
        self.model_calls += 1
        inp = getattr(u, "input_tokens", 0) or 0
        write = getattr(u, "cache_creation_input_tokens", 0) or 0
        read = getattr(u, "cache_read_input_tokens", 0) or 0
        self.input_tokens += inp
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.cache_write_tokens += write
        self.cache_read_tokens += read
        self.peak_context_tokens = max(self.peak_context_tokens, inp + write + read)


SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id TEXT PRIMARY KEY, task TEXT, system TEXT, model TEXT, limits TEXT, status TEXT,
    started_at TEXT, ended_at TEXT, error TEXT, iterations INTEGER, elapsed_s REAL, usage TEXT);
CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT);
CREATE TABLE IF NOT EXISTS tool_calls (
    tool_use_id TEXT PRIMARY KEY, iteration INTEGER, name TEXT, args TEXT, status TEXT,
    result TEXT, is_error INTEGER, started_at TEXT, ended_at TEXT, duration_s REAL);
CREATE TABLE IF NOT EXISTS errors (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, message TEXT);
"""


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit: every write is durable immediately
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- lifecycle ---------------------------------------------------------

    @classmethod
    def create(cls, path: Path, *, task: str, system: str, model: str, limits: Limits) -> "Session":
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"{path} already exists; resume it instead of creating it")
        s = cls(path)
        s.db.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}", redact(task), system,
                      model, json.dumps(asdict(limits)), "running", now(), None, None, 0, 0.0, json.dumps(asdict(Usage()))))
        return s

    @classmethod
    def load(cls, path: Path) -> "Session":
        if not path.is_file():
            raise FileNotFoundError(path)
        return cls(path)

    def _row(self):
        return self.db.execute("SELECT * FROM session").fetchone()

    id = property(lambda self: self._row()["id"])
    task = property(lambda self: self._row()["task"])
    system = property(lambda self: self._row()["system"])
    model = property(lambda self: self._row()["model"])
    status = property(lambda self: self._row()["status"])
    error = property(lambda self: self._row()["error"] or "")
    iterations = property(lambda self: self._row()["iterations"])
    elapsed_s = property(lambda self: self._row()["elapsed_s"])
    limits = property(lambda self: Limits(**json.loads(self._row()["limits"])))
    usage = property(lambda self: Usage(**json.loads(self._row()["usage"])))

    def set_limits(self, limits: Limits) -> None:
        self.db.execute("UPDATE session SET limits=?", (json.dumps(asdict(limits)),))

    def save_progress(self, *, iterations: int, usage: Usage, elapsed_s: float) -> None:
        self.db.execute("UPDATE session SET iterations=?, usage=?, elapsed_s=?",
                        (iterations, json.dumps(asdict(usage)), elapsed_s))

    def set_status(self, status: str, error: str = "") -> None:
        ended = None if status == "running" else now()
        self.db.execute("UPDATE session SET status=?, error=?, ended_at=?", (status, redact(error), ended))

    # --- conversation ------------------------------------------------------

    def messages(self) -> list[dict]:
        return [{"role": r["role"], "content": json.loads(r["content"])}
                for r in self.db.execute("SELECT role, content FROM messages ORDER BY seq")]

    def append_message(self, role: str, content) -> None:
        self.db.execute("INSERT INTO messages (role, content) VALUES (?,?)",
                        (role, redact(json.dumps(content, default=str))))

    # --- tool calls --------------------------------------------------------

    def tool_call(self, tool_use_id: str):
        return self.db.execute("SELECT * FROM tool_calls WHERE tool_use_id=?", (tool_use_id,)).fetchone()

    def begin_tool(self, tool_use_id: str, iteration: int, name: str, args: dict) -> None:
        self.db.execute("INSERT INTO tool_calls (tool_use_id, iteration, name, args, status, started_at) "
                        "VALUES (?,?,?,?,?,?)", (tool_use_id, iteration, name, redact(json.dumps(args)), "started", now()))

    def finish_tool(self, tool_use_id: str, result: str, is_error: bool, duration_s: float) -> None:
        self.db.execute("UPDATE tool_calls SET status='done', result=?, is_error=?, ended_at=?, duration_s=? "
                        "WHERE tool_use_id=?", (redact(result), int(is_error), now(), duration_s, tool_use_id))

    def mark_uncertain(self, tool_use_id: str) -> None:
        self.db.execute("UPDATE tool_calls SET status='uncertain' WHERE tool_use_id=?", (tool_use_id,))

    def count_tool_calls(self, name: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM tool_calls WHERE name=?", (name,)).fetchone()[0]

    def add_error(self, kind: str, message: str) -> None:
        self.db.execute("INSERT INTO errors (ts, kind, message) VALUES (?,?,?)", (now(), kind, redact(message)))

    # --- reporting ---------------------------------------------------------

    def artifacts(self, workspace: Path) -> list[str]:
        if not workspace.is_dir():
            return []
        return sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*")
                      if p.is_file() and not p.relative_to(workspace).parts[0].startswith("_"))

    def render_markdown(self, workspace: Path | None = None, cost_usd: float | None = None) -> str:
        s, u = self._row(), self.usage
        lines = [f"# Run {s['id']}", "",
                 f"- Status: **{s['status']}**" + (f" ({s['error']})" if s["error"] else ""),
                 f"- Model: {s['model']}",
                 f"- Started: {s['started_at']}   Ended: {s['ended_at'] or 'not ended'}",
                 f"- Model calls: {u.model_calls}   Iterations: {s['iterations']}   Active time: {s['elapsed_s']:.0f}s",
                 f"- Tokens: {u.input_tokens} input, {u.output_tokens} output, {u.cache_write_tokens} cache write, "
                 f"{u.cache_read_tokens} cache read; peak prompt {u.peak_context_tokens}"]
        if cost_usd is not None:
            lines.append(f"- Estimated cost: ${cost_usd:.4f}")
        lines += ["", "## Tool calls", "", "| # | iter | tool | status | duration | what |", "|---|---|---|---|---|---|"]
        for i, r in enumerate(self.db.execute("SELECT * FROM tool_calls ORDER BY started_at"), 1):
            args = json.loads(r["args"])
            what = (args.get("path") or args.get("command") or (args.get("code") or "").strip().splitlines()[:1] or [""])
            what = what if isinstance(what, str) else what[0]
            status = r["status"] + (" (error)" if r["is_error"] else "")
            lines.append(f"| {i} | {r['iteration']} | {r['name']} | {status} | "
                         f"{(r['duration_s'] or 0):.1f}s | `{what[:70].replace('|', '/')}` |")
        errs = self.db.execute("SELECT * FROM errors ORDER BY id").fetchall()
        if errs:
            lines += ["", "## Infrastructure errors", ""] + [f"- {e['ts']} **{e['kind']}**: {e['message'][:200]}" for e in errs]
        if workspace is not None:
            lines += ["", "## Artifacts", ""] + [f"- `{a}`" for a in self.artifacts(workspace)]
        return "\n".join(lines) + "\n"
