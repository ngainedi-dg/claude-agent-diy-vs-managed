"""Tests for the DIY loop, sessions and tools. A scripted fake client stands in for the API; no Docker needed."""

import json
from types import SimpleNamespace

import anthropic
import pytest

from diy.agent_loop import (API_RETRY_DELAYS_S, UNCERTAIN, Tracer, block_to_dict, cost_usd, pending_tool_uses,
                            run_agent)
from diy.sandbox import ExecResult, SandboxError
from diy.sessions import RESUMABLE, Limits, Session, Usage, redact
from diy.tools import TOOLS, Dispatcher, ToolError, resolve_path

MODEL = "claude-sonnet-5-5"


# --- fakes -----------------------------------------------------------------

class FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0):
        self.input_tokens, self.output_tokens = input_tokens, output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens

    def model_dump(self):
        return dict(vars(self))


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id, name="execute_python", **args):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=args)


def reply(*blocks, stop_reason="tool_use", usage=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=usage or FakeUsage())


class SimulatedCrash(BaseException):
    """Stands in for kill -9: nothing in the loop catches it, so nothing gets a chance to clean up."""


class FakeClient:
    """Plays back scripted responses (the last one repeats) and remembers what it was sent."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": json.loads(json.dumps(kwargs["messages"]))})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


class FakeDispatcher:
    definitions = TOOLS

    def __init__(self, fn=None):
        self.runs = []
        self.fn = fn or (lambda name, args: (f"ran {name}", False))

    def run(self, name, args):
        self.runs.append((name, args))
        return self.fn(name, args)


def new_session(tmp_path, limits=None):
    return Session.create(tmp_path / "session.sqlite", task="do it", system="sys", model=MODEL,
                          limits=limits or Limits())


def reopen(tmp_path):
    return Session.load(tmp_path / "session.sqlite")


def go(client, tmp_path, dispatcher=None, limits=None, session=None, tracer=None, sleep=lambda s: None):
    session = session or new_session(tmp_path, limits)
    return run_agent(client, session=session, dispatcher=dispatcher or FakeDispatcher(),
                     tracer=tracer or Tracer(None), log=lambda *_: None, sleep=sleep)


def api_error(status):
    response = SimpleNamespace(request=SimpleNamespace(), status_code=status, headers={})
    return anthropic.APIStatusError(f"http {status}", response=response, body=None)


# --- loop ------------------------------------------------------------------

def test_tool_round_trip_then_completion(tmp_path):
    client = FakeClient(reply(text("working"), tool_use("t1", code="print(1)")),
                        reply(text("all done"), stop_reason="end_turn"))
    result = go(client, tmp_path)
    assert result.status == "completed" and result.final_text == "all done" and result.iterations == 2
    second_call = client.calls[1]["messages"]
    assert [m["role"] for m in second_call] == ["user", "assistant", "user"]
    assert second_call[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ran execute_python", "is_error": False}]
    assert client.calls[0]["cache_control"] == {"type": "ephemeral"}


def test_multiple_tool_calls_return_in_one_user_message(tmp_path):
    client = FakeClient(reply(tool_use("a", code="1"), tool_use("b", "list_files")),
                        reply(text("ok"), stop_reason="end_turn"))
    go(client, tmp_path)
    results = client.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert len(client.calls[1]["messages"]) == 3


def test_tool_errors_are_given_back_to_the_model(tmp_path):
    client = FakeClient(reply(tool_use("t1", code="boom")), reply(text("fixed"), stop_reason="end_turn"))
    result = go(client, tmp_path, FakeDispatcher(lambda n, a: ("Traceback ... SyntaxError", True)))
    assert result.status == "completed"
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_bug_in_a_tool_does_not_crash_the_run(tmp_path):
    def buggy(name, args):
        raise ValueError("oops")
    client = FakeClient(reply(tool_use("t1", code="1")), reply(text("done"), stop_reason="end_turn"))
    session = new_session(tmp_path)
    assert go(client, tmp_path, FakeDispatcher(buggy), session=session).status == "completed"
    assert "internal tool error: ValueError" in client.calls[1]["messages"][-1]["content"][0]["content"]
    assert session.db.execute("SELECT kind FROM errors").fetchone()["kind"] == "tool"


def test_iteration_limit(tmp_path):
    client = FakeClient(*[reply(tool_use(f"t{i}", code="1")) for i in range(5)])
    result = go(client, tmp_path, limits=Limits(max_iterations=3))
    assert result.status == "max_iterations" and len(client.calls) == 3


def test_budget_limit_stops_before_the_next_call(tmp_path):
    big = FakeUsage(input_tokens=1_000_000)  # $2 at Sonnet prices
    client = FakeClient(reply(tool_use("t", code="1"), usage=big))
    result = go(client, tmp_path, limits=Limits(max_cost_usd=1.0))
    assert result.status == "budget_exhausted" and len(client.calls) == 1
    assert result.cost_usd == pytest.approx(2.0001)  # 1M input + 10 output tokens


def test_duration_limit(tmp_path):
    result = go(FakeClient(reply(tool_use("t", code="1"))), tmp_path, limits=Limits(max_seconds=-1))
    assert result.status == "max_duration"


def test_sandbox_failure_is_distinct_from_tool_failure(tmp_path):
    def broken(name, args):
        raise SandboxError("container gone")
    session = new_session(tmp_path)
    result = go(FakeClient(reply(tool_use("t", code="1"))), tmp_path, FakeDispatcher(broken), session=session)
    assert result.status == "sandbox_error" and "container gone" in result.error
    assert session.db.execute("SELECT kind FROM errors").fetchone()["kind"] == "sandbox"
    assert session.status in RESUMABLE


def test_same_tool_use_id_is_never_executed_twice(tmp_path):
    client = FakeClient(reply(tool_use("same", code="1")), reply(tool_use("same", code="1")),
                        reply(text("done"), stop_reason="end_turn"))
    d = FakeDispatcher()
    result = go(client, tmp_path, d)
    assert result.status == "completed" and len(d.runs) == 1


def test_truncated_tool_call_is_not_executed(tmp_path):
    client = FakeClient(reply(tool_use("t", code="print("), stop_reason="max_tokens"),
                        reply(text("done"), stop_reason="end_turn"))
    d = FakeDispatcher()
    go(client, tmp_path, d)
    assert d.runs == []
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_truncated_text_gets_a_continue_prompt(tmp_path):
    client = FakeClient(reply(text("partial"), stop_reason="max_tokens"), reply(text("done"), stop_reason="end_turn"))
    assert go(client, tmp_path).status == "completed"
    assert "output limit" in client.calls[1]["messages"][-1]["content"]


def test_refusal_and_unexpected_stop(tmp_path):
    assert go(FakeClient(reply(stop_reason="refusal")), tmp_path).status == "refusal"
    (tmp_path / "b").mkdir()
    assert go(FakeClient(reply(stop_reason="stop_sequence")), tmp_path / "b").status == "unexpected_stop"


def test_usage_and_cost_accounting():
    u = Usage()
    u.add(FakeUsage(1000, 100, 200, 5000))
    u.add(FakeUsage(10, 5, 0, 100))
    assert u.model_calls == 2 and u.peak_context_tokens == 6200
    assert cost_usd(u, MODEL) == pytest.approx((1010 * 2 + 105 * 10 + 200 * 2.5 + 5100 * 0.2) / 1e6)


# --- API failures ----------------------------------------------------------

def test_transient_api_errors_are_retried_with_backoff(tmp_path):
    client = FakeClient(api_error(529), api_error(500), reply(text("done"), stop_reason="end_turn"))
    waits, session = [], new_session(tmp_path)
    result = go(client, tmp_path, session=session, sleep=waits.append)
    assert result.status == "completed" and waits == list(API_RETRY_DELAYS_S[:2])
    assert session.db.execute("SELECT COUNT(*) FROM errors WHERE kind='api'").fetchone()[0] == 2


def test_non_retryable_api_error_fails_immediately(tmp_path):
    waits = []
    result = go(FakeClient(api_error(400)), tmp_path, sleep=waits.append)
    assert result.status == "api_error" and waits == [] and "400" in result.error


def test_retries_are_bounded(tmp_path):
    client = FakeClient(api_error(529))
    waits = []
    result = go(client, tmp_path, sleep=waits.append)
    assert result.status == "api_error" and waits == list(API_RETRY_DELAYS_S)
    assert len(client.calls) == len(API_RETRY_DELAYS_S) + 1


def test_api_failure_leaves_a_resumable_session(tmp_path):
    session = new_session(tmp_path)
    go(FakeClient(api_error(401)), tmp_path, session=session)
    assert session.status == "api_error" and session.status in RESUMABLE


# --- sessions and recovery -------------------------------------------------

def test_session_records_everything_needed_to_inspect_a_run(tmp_path):
    client = FakeClient(reply(tool_use("t1", code="print(1)"), usage=FakeUsage(50, 5)),
                        reply(text("done"), stop_reason="end_turn", usage=FakeUsage(70, 7)))
    go(client, tmp_path)
    again = reopen(tmp_path)
    assert again.status == "completed" and again.iterations == 2
    assert again.usage.input_tokens == 120 and again.usage.output_tokens == 12
    assert [m["role"] for m in again.messages()] == ["user", "assistant", "user", "assistant"]
    row = again.tool_call("t1")
    assert row["status"] == "done" and row["result"] == "ran execute_python"
    assert json.loads(row["args"]) == {"code": "print(1)"}
    md = again.render_markdown(tmp_path)
    assert "completed" in md and "execute_python" in md and "print(1)" in md


def test_response_blocks_round_trip_through_the_database(tmp_path):
    """Thinking blocks must come back exactly as received; check with the SDK's real response types."""
    original = [{"type": "thinking", "thinking": "", "signature": "sig=="},
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "toolu_1", "name": "list_files", "input": {"path": "/data"}}]
    message = anthropic.types.Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": MODEL, "content": original,
        "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
    dumped = [block_to_dict(b) for b in message.content]
    assert dumped == original
    new_session(tmp_path).append_message("assistant", dumped)
    assert reopen(tmp_path).messages()[0]["content"] == original


def test_crash_during_a_tool_is_recovered_without_rerunning_it(tmp_path):
    def flaky(name, args):
        if args["code"] == "t1":
            raise SimulatedCrash()
        return f"ran {args['code']}", False

    with pytest.raises(SimulatedCrash):
        go(FakeClient(reply(tool_use("a", code="ok1"), tool_use("b", code="t1"), tool_use("c", code="never_started"))),
           tmp_path, FakeDispatcher(flaky))
    crashed = reopen(tmp_path)
    assert crashed.status == "running"                      # nobody got to record an ending
    assert crashed.tool_call("a")["status"] == "done"
    assert crashed.tool_call("b")["status"] == "started"    # may or may not have taken effect
    assert crashed.tool_call("c") is None                   # never began

    client = FakeClient(reply(text("done"), stop_reason="end_turn"))
    second = FakeDispatcher(lambda n, a: (f"ran {a['code']}", False))
    result = go(client, tmp_path, second, session=crashed)

    assert result.status == "completed"
    assert [a["code"] for _, a in second.runs] == ["never_started"]  # a: reused, b: uncertain, c: run now
    results = client.calls[0]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b", "c"]
    assert results[0]["content"] == "ran ok1" and not results[0]["is_error"]
    assert results[1]["content"] == UNCERTAIN and results[1]["is_error"]
    assert results[2]["content"] == "ran never_started"
    assert reopen(tmp_path).tool_call("b")["status"] == "uncertain"


def test_crash_during_a_model_call_resumes_by_asking_again(tmp_path):
    with pytest.raises(SimulatedCrash):
        go(FakeClient(SimulatedCrash()), tmp_path)
    client = FakeClient(reply(text("done"), stop_reason="end_turn"))
    assert go(client, tmp_path, session=reopen(tmp_path)).status == "completed"
    assert [m["role"] for m in client.calls[0]["messages"]] == ["user"]  # the task is not duplicated


def test_resume_continues_counts_and_accumulates_usage(tmp_path):
    session = new_session(tmp_path, Limits(max_iterations=1))
    first = go(FakeClient(reply(tool_use("t0", code="1"), usage=FakeUsage(1000, 100))), tmp_path, session=session)
    assert first.status == "max_iterations" and reopen(tmp_path).status in RESUMABLE

    again = reopen(tmp_path)
    again.set_limits(Limits(max_iterations=5))
    second = go(FakeClient(reply(text("done"), stop_reason="end_turn", usage=FakeUsage(1000, 100))), tmp_path,
                session=again)
    assert second.status == "completed" and second.iterations == 2
    assert second.usage.model_calls == 2 and second.usage.input_tokens == 2000


def test_budget_exhausted_session_can_be_resumed_with_more_budget(tmp_path):
    session = new_session(tmp_path, Limits(max_cost_usd=0.001))
    go(FakeClient(reply(tool_use("t", code="1"), usage=FakeUsage(10_000, 100))), tmp_path, session=session)
    assert session.status == "budget_exhausted"
    again = reopen(tmp_path)
    again.set_limits(Limits(max_cost_usd=5.0))
    assert go(FakeClient(reply(text("done"), stop_reason="end_turn")), tmp_path, session=again).status == "completed"


def test_ctrl_c_marks_the_session_interrupted_and_resumable(tmp_path):
    def interrupt(name, args):
        raise KeyboardInterrupt()
    session = new_session(tmp_path)
    result = go(FakeClient(reply(tool_use("t", code="1"))), tmp_path, FakeDispatcher(interrupt), session=session)
    assert result.status == "interrupted" and session.status in RESUMABLE
    assert session.tool_call("t")["status"] == "started"


def test_completed_and_refused_sessions_are_not_resumable():
    assert "completed" not in RESUMABLE and "refusal" not in RESUMABLE


def test_pending_tool_uses():
    assert pending_tool_uses([]) == []
    assert pending_tool_uses([{"role": "user", "content": "x"}]) == []
    msgs = [{"role": "assistant", "content": [{"type": "text", "text": "t"}, {"type": "tool_use", "id": "a"}]}]
    assert [b["id"] for b in pending_tool_uses(msgs)] == ["a"]
    assert pending_tool_uses(msgs + [{"role": "user", "content": []}]) == []


# --- secrets -----------------------------------------------------------------

def test_redaction_covers_key_patterns_and_environment_secrets(monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "hunter2-hunter2-hunter2")
    monkeypatch.setenv("HARMLESS_NAME", "not-a-secret-value-at-all")
    cleaned = redact("sk-ant-api03-ABC_def and hunter2-hunter2-hunter2 and not-a-secret-value-at-all")
    assert cleaned == "[REDACTED] and [REDACTED] and not-a-secret-value-at-all"


def test_trace_and_session_never_contain_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-REALLYSECRETVALUE-123456")
    client = FakeClient(reply(tool_use("t", code="key = 'sk-ant-api03-REALLYSECRETVALUE-123456'")),
                        reply(text("done"), stop_reason="end_turn"))
    session = new_session(tmp_path)
    go(client, tmp_path, FakeDispatcher(lambda n, a: ("echo sk-ant-abc123 leaked", False)), session=session,
       tracer=Tracer(tmp_path / "t" / "trace.jsonl"))
    raw = (tmp_path / "t" / "trace.jsonl").read_text()
    events = [json.loads(line)["event"] for line in raw.splitlines()]
    assert events == ["run_start", "model_response", "tool_call", "model_response", "run_end"]
    dump = raw + "\n".join(session.db.iterdump())
    assert "REALLYSECRET" not in dump and "sk-ant-abc123" not in dump and "[REDACTED]" in dump


# --- tools -----------------------------------------------------------------

class FakeSandbox:
    def __init__(self):
        self.calls = []

    def exec(self, argv, input=None, timeout=120):
        self.calls.append((argv, input, timeout))
        return ExecResult("out", "", 0, 0.1)


@pytest.mark.parametrize("path,expected", [
    ("report.md", "/workspace/report.md"),
    ("/workspace/charts/a.png", "/workspace/charts/a.png"),
    ("/workspace/a/../b.txt", "/workspace/b.txt"),
    ("/data/sales_transactions.csv", "/data/sales_transactions.csv"),
])
def test_resolve_path_accepts(path, expected):
    assert resolve_path(path) == expected


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "/workspace/../etc/passwd", "/workspacefoo", "", "/data/../etc"])
def test_resolve_path_rejects(path):
    with pytest.raises(ToolError):
        resolve_path(path)


def test_data_mount_is_not_writable():
    with pytest.raises(ToolError):
        resolve_path("/data/x.csv", write=True)


def test_dispatcher_validates_before_running():
    sb = FakeSandbox()
    d = Dispatcher(sb)
    for name, args in [("nope", {}), ("execute_python", {}), ("execute_python", {"code": 5}),
                       ("read_file", {"path": "x", "limit": 10**9}), ("list_files", {"extra": 1})]:
        assert d.run(name, args)[1], (name, args)
    assert sb.calls == []


def test_dispatcher_blocks_escapes_and_unlisted_commands():
    d = Dispatcher(FakeSandbox())
    assert d.run("write_file", {"path": "/etc/cron.d/x", "content": "x"})[1]
    assert d.run("write_file", {"path": "/data/new.csv", "content": "x"})[1]
    assert d.run("read_file", {"path": "../../etc/shadow"})[1]
    assert "not allowed" in d.run("run_command", {"command": "curl http://example.com"})[0]
    assert d.run("run_command", {"command": "ls -la 'unterminated"})[1]


def test_run_command_does_not_use_a_shell():
    sb = FakeSandbox()
    Dispatcher(sb).run("run_command", {"command": "ls /workspace; rm -rf /"})
    assert sb.calls[0][0] == ["ls", "/workspace;", "rm", "-rf", "/"]  # ';' is just an argument


def test_execute_python_stages_a_numbered_script_and_numbering_survives_resume():
    sb = FakeSandbox()
    d = Dispatcher(sb)
    d.run("execute_python", {"code": "print(1)"})
    d.run("execute_python", {"code": "print(2)", "timeout_seconds": 5})
    runs = [c for c in sb.calls if c[0][0] == "python"]
    assert runs[0][0][1].endswith("step_001.py") and runs[1][0][1].endswith("step_002.py")
    assert runs[1][2] == 5
    sb2 = FakeSandbox()
    Dispatcher(sb2, python_calls=2).run("execute_python", {"code": "print(3)"})
    assert [c for c in sb2.calls if c[0][0] == "python"][0][0][1].endswith("step_003.py")


# --- command line ------------------------------------------------------------

@pytest.mark.parametrize("status,code", [("completed", 2), ("refusal", 2)])
def test_resume_refuses_sessions_that_are_finished(tmp_path, monkeypatch, status, code):
    from diy.main import main
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    session = Session.create(tmp_path / "session.sqlite", task="t", system="s", model=MODEL, limits=Limits())
    session.set_status(status)
    assert main(["--resume", str(tmp_path)]) == code
