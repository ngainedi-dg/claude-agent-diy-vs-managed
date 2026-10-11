"""Runtime metrics, in one shape for both implementations.

Reads what each implementation recorded for a run and returns the same fields. A value that
could not be observed is None, never 0. Costs are computed here, from token counts, with one
price table for both sides; the Managed platform's own reported figure is kept alongside.

Run directory layout (see diy/main.py and managed/main.py): <run>/run.json, <run>/outputs/...
"""

import json
import sqlite3
from pathlib import Path

# USD per million tokens: input, output, 5-minute cache write, cache read.
# Sonnet 5.5 and Opus 5.5 cache reads are 0.05x input. 1-hour cache writes cost 2x input.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
}
RUNTIME_USD_PER_HOUR = 0.08  # Managed Agents session runtime, billed while the session is running
COMPLETED = {"completed"}


def token_cost_usd(model: str, inp, out, cache_write_5m, cache_write_1h, cache_read) -> float | None:
    values = (inp, out, cache_write_5m, cache_write_1h, cache_read)
    if model not in PRICES or any(v is None for v in values):
        return None
    p_in, p_out, p_write, p_read = PRICES[model]
    return (inp * p_in + out * p_out + cache_write_5m * p_write + cache_write_1h * 2 * p_in + cache_read * p_read) / 1e6


def _diy_counts(run: Path) -> tuple[int | None, int | None]:
    db = run / "session.sqlite"
    if not db.is_file():
        return None, None
    con = sqlite3.connect(db)
    try:
        return (con.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM errors").fetchone()[0])
    finally:
        con.close()


def _artifact_count(run: Path) -> int:
    out = run / "outputs"
    if not out.is_dir():
        return 0
    return sum(1 for p in out.rglob("*") if p.is_file() and not p.relative_to(out).parts[0].startswith("_"))


def load_run_metrics(run: Path) -> dict:
    """Normalised metrics for one run directory (the parent of outputs/)."""
    info = json.loads((run / "run.json").read_text(encoding="utf-8"))
    impl, model = info["implementation"], info["model"]
    m = {"implementation": impl, "run": run.name, "model": model, "status": info["status"],
         "completed": info["status"] in COMPLETED, "artifact_files": _artifact_count(run)}

    if impl == "diy":
        u = info["usage"]
        tool_calls, errors = _diy_counts(run)
        m.update(active_seconds=info.get("active_seconds"), model_calls=u.get("model_calls"), tool_calls=tool_calls,
                 input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                 cache_write_tokens=u["cache_write_tokens"], cache_read_tokens=u["cache_read_tokens"],
                 peak_context_tokens=u.get("peak_context_tokens"), infrastructure_errors=errors,
                 runtime_cost_usd=None,  # our own compute is not billed by Anthropic and was not measured
                 reported_list_cost_usd=None)
        m["token_cost_usd"] = token_cost_usd(model, u["input_tokens"], u["output_tokens"], u["cache_write_tokens"], 0,
                                             u["cache_read_tokens"])
    elif impl == "managed":
        x = info["metrics"]
        s = x["session_usage"]
        active = x.get("active_seconds")
        m.update(active_seconds=active, model_calls=x.get("model_calls"), tool_calls=x.get("tool_calls"),
                 input_tokens=s.get("input_tokens"), output_tokens=s.get("output_tokens"),
                 cache_write_tokens=None if s.get("cache_write_5m_tokens") is None
                 else s["cache_write_5m_tokens"] + (s.get("cache_write_1h_tokens") or 0),
                 cache_read_tokens=s.get("cache_read_input_tokens"), peak_context_tokens=None,
                 infrastructure_errors=len(info.get("errors", [])),
                 runtime_cost_usd=None if active is None else round(active / 3600 * RUNTIME_USD_PER_HOUR, 4),
                 reported_list_cost_usd=None if x.get("list_cost_usd_reported") is None
                 else float(x["list_cost_usd_reported"]))
        m["token_cost_usd"] = token_cost_usd(model, s.get("input_tokens"), s.get("output_tokens"),
                                             s.get("cache_write_5m_tokens"), s.get("cache_write_1h_tokens"),
                                             s.get("cache_read_input_tokens"))
    else:
        raise ValueError(f"unknown implementation {impl!r} in {run}")

    parts = [m["token_cost_usd"], m["runtime_cost_usd"]]
    known = [p for p in parts if p is not None]
    m["total_cost_usd"] = None if m["token_cost_usd"] is None else round(sum(known), 4)  # API plus platform charges
    if m["token_cost_usd"] is not None:
        m["token_cost_usd"] = round(m["token_cost_usd"], 4)
    return m
