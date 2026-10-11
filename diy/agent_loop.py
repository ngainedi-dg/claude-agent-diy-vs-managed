"""The agent execution loop, written by hand against the Messages API.

One pass of the loop: send the conversation, look at stop_reason, run any
requested tools, send the results back. It ends when the model stops asking for
tools or when a limit is hit. No agent framework and no SDK tool runner.

All state lives in the Session (SQLite), so a run can be inspected afterwards and
resumed after an interruption. Messages are kept as plain dicts, the same shape the
API takes, whether they came from a live response or from the database.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from diy.sandbox import SandboxError
from diy.sessions import Session, Usage, now, redact
from diy.tools import Dispatcher

# USD per million tokens: input, output, 5-minute cache write, cache read.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),  # cache reads are 0.05x input on Sonnet 5.5 and Opus 5.5
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
}
API_RETRY_DELAYS_S = (5, 20, 60)  # on top of the SDK's own two quick retries
UNCERTAIN = ("This call was interrupted before a result was recorded, so it may or may not have taken effect. "
             "It has not been run again. Inspect the workspace to see what state it left, then decide whether to retry.")


def cost_usd(usage: Usage, model: str) -> float:
    inp, out, write, read = PRICES[model]
    return (usage.input_tokens * inp + usage.output_tokens * out
            + usage.cache_write_tokens * write + usage.cache_read_tokens * read) / 1_000_000


@dataclass
class RunResult:
    status: str  # completed | max_iterations | max_duration | budget_exhausted | refusal | api_error | sandbox_error | unexpected_stop | interrupted
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
        line = redact(json.dumps({"ts": now(), "event": event, **data}, default=str))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def block_to_dict(block) -> dict:
    """A response content block as the plain dict the API accepts back (unset fields stay unset)."""
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_unset=True)
    return dict(vars(block))


def text_of(content: list[dict]) -> str:
    return "\n".join(b["text"] for b in content if b["type"] == "text")


def is_retryable(e: anthropic.APIError) -> bool:
    if isinstance(e, anthropic.APIConnectionError):  # includes timeouts
        return True
    return isinstance(e, anthropic.APIStatusError) and (e.status_code in (408, 409, 429) or e.status_code >= 500)


def pending_tool_uses(messages: list[dict]) -> list[dict]:
    """tool_use blocks in the last assistant message that have not been answered yet."""
    last = messages[-1] if messages else None
    if last and last["role"] == "assistant" and isinstance(last["content"], list):
        return [b for b in last["content"] if b["type"] == "tool_use"]
    return []


def run_agent(client, *, session: Session, dispatcher: Dispatcher, tracer: Tracer, max_tokens: int = 16000,
              log=print, sleep=time.sleep) -> RunResult:
    model, system, limits = session.model, session.system, session.limits
    usage, iteration = session.usage, session.iterations
    messages = session.messages()
    base_elapsed, started = session.elapsed_s, time.monotonic()
    elapsed = lambda: base_elapsed + time.monotonic() - started
    resumed = bool(messages)

    def add_message(role, content):
        messages.append({"role": role, "content": content})
        session.append_message(role, content)

    def checkpoint():
        session.save_progress(iterations=iteration, usage=usage, elapsed_s=elapsed())

    def finish(status, error="", final_text=""):
        checkpoint()
        session.set_status(status, error)
        result = RunResult(status, iteration, usage, cost_usd(usage, model), elapsed(), final_text, error, messages)
        tracer.emit("run_end", status=status, iterations=iteration, error=error, usage=vars(usage),
                    cost_usd=round(result.cost_usd, 4), duration_s=round(result.duration_s, 1))
        return result

    def call_model():
        """One model call. Transient failures are retried with backoff; anything else is raised."""
        for attempt in range(len(API_RETRY_DELAYS_S) + 1):
            try:
                return client.messages.create(
                    model=model, max_tokens=max_tokens, system=system, tools=dispatcher.definitions,
                    messages=messages, cache_control={"type": "ephemeral"})
            except anthropic.APIError as e:
                session.add_error("api", f"{type(e).__name__}: {e}")
                tracer.emit("api_error", iteration=iteration, attempt=attempt, error_type=type(e).__name__,
                            retryable=is_retryable(e), error=str(e))
                if not is_retryable(e) or attempt == len(API_RETRY_DELAYS_S):
                    raise
                log(f"    API error ({type(e).__name__}); retrying in {API_RETRY_DELAYS_S[attempt]}s")
                sleep(API_RETRY_DELAYS_S[attempt])

    def answer_tool_calls(tool_uses):
        """Run (or recover) each requested call. All results go back in one user message."""
        results = []
        for block in tool_uses:
            row = session.tool_call(block["id"])
            if row and row["status"] == "done":  # already executed (resumed session): reuse, never re-run
                text, is_error = row["result"], bool(row["is_error"])
            elif row:  # started but never finished: outcome unknown, so do not run it again
                session.mark_uncertain(block["id"])
                text, is_error = UNCERTAIN, True
                tracer.emit("uncertain_tool_call", iteration=iteration, id=block["id"], tool=block["name"])
                log(f"    {block['name']}: interrupted earlier, not re-run")
            else:
                t0 = time.monotonic()
                session.begin_tool(block["id"], iteration, block["name"], block["input"])
                try:
                    text, is_error = dispatcher.run(block["name"], block["input"])
                except SandboxError as e:  # the sandbox broke, not the agent's code; the call stays "started"
                    session.add_error("sandbox", str(e))
                    tracer.emit("sandbox_error", iteration=iteration, tool=block["name"], error=str(e))
                    raise
                except Exception as e:  # a bug in our own tool code; tell the model rather than crash the run
                    session.add_error("tool", f"{block['name']}: {type(e).__name__}: {e}")
                    text, is_error = f"internal tool error: {type(e).__name__}: {e}", True
                session.finish_tool(block["id"], text, is_error, time.monotonic() - t0)
                tracer.emit("tool_call", iteration=iteration, id=block["id"], tool=block["name"], args=block["input"],
                            is_error=is_error, duration_s=round(time.monotonic() - t0, 2), result=text)
                log(f"    {block['name']} -> {'error' if is_error else 'ok'} ({time.monotonic() - t0:.1f}s)")
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": text, "is_error": is_error})
            checkpoint()
        add_message("user", results)

    tracer.emit("run_resume" if resumed else "run_start", model=model, limits=vars(limits),
                tools=[t["name"] for t in dispatcher.definitions], iteration=iteration)
    session.set_status("running")
    if not resumed:
        add_message("user", session.task)

    try:
        while True:
            if pending := pending_tool_uses(messages):  # first pass after a resume, or after a tool_use response
                answer_tool_calls(pending)

            if iteration >= limits.max_iterations:
                return finish("max_iterations")
            if elapsed() > limits.max_seconds:
                return finish("max_duration")
            if cost_usd(usage, model) >= limits.max_cost_usd:
                return finish("budget_exhausted")

            iteration += 1
            t0 = time.monotonic()
            try:
                response = call_model()
            except anthropic.APIError as e:
                iteration -= 1
                return finish("api_error", f"{type(e).__name__}: {e}")
            usage.add(response.usage)
            # The whole content list goes back unchanged, so thinking blocks stay valid.
            content = [block_to_dict(b) for b in response.content]
            add_message("assistant", content)
            checkpoint()
            tool_uses = [b for b in content if b["type"] == "tool_use"]
            tracer.emit("model_response", iteration=iteration, stop_reason=response.stop_reason,
                        duration_s=round(time.monotonic() - t0, 2), text=text_of(content),
                        tool_calls=[b["name"] for b in tool_uses], usage=response.usage.model_dump())
            log(f"[{iteration}] {response.stop_reason}: " + (", ".join(b["name"] for b in tool_uses) or "(no tool calls)"))

            if response.stop_reason == "end_turn":
                return finish("completed", final_text=text_of(content))
            if response.stop_reason == "refusal":
                return finish("refusal", "model declined to continue")
            if response.stop_reason == "pause_turn":
                continue  # nothing to answer; the assistant turn is already last in the history
            if response.stop_reason == "max_tokens":
                if tool_uses:  # the call may be cut off mid-argument, so it is not run
                    add_message("user", [{"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                                          "content": "Not run: your response was truncated by the output limit. "
                                                     "Retry with less content."} for b in tool_uses])
                else:
                    add_message("user", "Your last response hit the output limit. Continue, "
                                        "writing long files in smaller pieces.")
                continue
            if response.stop_reason != "tool_use":
                return finish("unexpected_stop", f"stop_reason={response.stop_reason}")
            # stop_reason == "tool_use": the top of the loop answers the calls
    except KeyboardInterrupt:
        return finish("interrupted", "interrupted by the user (Ctrl-C)")
    except SandboxError as e:
        return finish("sandbox_error", str(e))
