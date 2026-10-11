"""The agent execution loop, written by hand against the Messages API.

One pass of the loop: send the conversation, look at stop_reason, run any
requested tools, send the results back. It ends when the model stops asking for
tools or when a limit is hit. No agent framework and no SDK tool runner.
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from diy.sandbox import SandboxError
from diy.tools import Dispatcher

# USD per million tokens: input, output, 5-minute cache write, cache read.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
}
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


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

    def add(self, u) -> None:
        self.model_calls += 1
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0

    def cost_usd(self, model: str) -> float:
        inp, out, write, read = PRICES[model]
        return (self.input_tokens * inp + self.output_tokens * out
                + self.cache_write_tokens * write + self.cache_read_tokens * read) / 1_000_000


@dataclass
class RunResult:
    status: str  # completed | max_iterations | max_duration | budget_exhausted | refusal | api_error | sandbox_error | unexpected_stop
    iterations: int
    usage: Usage
    cost_usd: float
    duration_s: float
    final_text: str = ""
    error: str = ""
    messages: list = field(default_factory=list)


class Tracer:
    """Append-only JSONL trace. Secrets are redacted before anything is written."""

    def __init__(self, path: Path | None):
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **data) -> None:
        if not self.path:
            return
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event, **data}
        line = SECRET.sub("[REDACTED]", json.dumps(record, default=str))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def text_of(content) -> str:
    return "\n".join(b.text for b in content if b.type == "text")


def run_agent(client, *, model: str, system: str, task: str, dispatcher: Dispatcher, limits: Limits,
              tracer: Tracer, max_tokens: int = 16000, log=print) -> RunResult:
    messages = [{"role": "user", "content": task}]
    usage = Usage()
    executed: dict[str, tuple[str, bool]] = {}  # tool_use id -> result; a call is never run twice
    started = time.monotonic()
    tracer.emit("run_start", model=model, limits=vars(limits), tools=[t["name"] for t in dispatcher.definitions])

    def finish(status, iterations, error="", final_text=""):
        result = RunResult(status, iterations, usage, usage.cost_usd(model), time.monotonic() - started,
                           final_text, error, messages)
        tracer.emit("run_end", status=status, iterations=iterations, error=error,
                    usage=vars(usage), cost_usd=round(result.cost_usd, 4), duration_s=round(result.duration_s, 1))
        return result

    for iteration in range(1, limits.max_iterations + 1):
        if time.monotonic() - started > limits.max_seconds:
            return finish("max_duration", iteration - 1)
        if usage.cost_usd(model) >= limits.max_cost_usd:
            return finish("budget_exhausted", iteration - 1)

        t0 = time.monotonic()
        try:
            # The SDK already retries 408/409/429/5xx twice with backoff.
            response = client.messages.create(
                model=model, max_tokens=max_tokens, system=system, tools=dispatcher.definitions,
                messages=messages, cache_control={"type": "ephemeral"})
        except anthropic.APIError as e:
            tracer.emit("api_error", iteration=iteration, error_type=type(e).__name__, error=str(e))
            return finish("api_error", iteration - 1, f"{type(e).__name__}: {e}")
        usage.add(response.usage)
        # The whole content list goes back unchanged, so thinking blocks stay valid.
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        tracer.emit("model_response", iteration=iteration, stop_reason=response.stop_reason,
                    duration_s=round(time.monotonic() - t0, 2), text=text_of(response.content),
                    tool_calls=[b.name for b in tool_uses], usage=response.usage.model_dump())
        log(f"[{iteration}] {response.stop_reason}: " + (", ".join(b.name for b in tool_uses) or "(no tool calls)"))

        if response.stop_reason == "end_turn":
            return finish("completed", iteration, final_text=text_of(response.content))
        if response.stop_reason == "refusal":
            return finish("refusal", iteration, "model declined to continue")
        if response.stop_reason == "pause_turn":
            continue  # nothing to answer; the assistant turn is already last in the history
        if response.stop_reason == "max_tokens" and not tool_uses:
            messages.append({"role": "user", "content": "Your last response hit the output limit. Continue, "
                             "writing long files in smaller pieces."})
            continue
        if response.stop_reason not in ("tool_use", "max_tokens"):
            return finish("unexpected_stop", iteration, f"stop_reason={response.stop_reason}")

        # All results for one assistant turn go back in a single user message.
        results = []
        for block in tool_uses:
            if response.stop_reason == "max_tokens":  # the call may be cut off mid-argument
                text, is_error = "Not run: your response was truncated by the output limit. Retry with less content.", True
            elif block.id in executed:
                text, is_error = executed[block.id]
            else:
                t1 = time.monotonic()
                try:
                    text, is_error = dispatcher.run(block.name, block.input)
                except SandboxError as e:  # the sandbox broke, not the agent's code
                    tracer.emit("sandbox_error", iteration=iteration, tool=block.name, error=str(e))
                    return finish("sandbox_error", iteration, str(e))
                executed[block.id] = (text, is_error)
                tracer.emit("tool_call", iteration=iteration, id=block.id, tool=block.name, args=block.input,
                            is_error=is_error, duration_s=round(time.monotonic() - t1, 2), result=text)
                log(f"    {block.name} -> {'error' if is_error else 'ok'} ({time.monotonic() - t1:.1f}s)")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    return finish("max_iterations", limits.max_iterations)
