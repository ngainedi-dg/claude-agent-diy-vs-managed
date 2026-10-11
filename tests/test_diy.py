"""Tests for the DIY agent. The loop tests use a scripted fake client: no API calls, no Docker."""

import json
import shutil
import subprocess
from types import SimpleNamespace

import anthropic
import pytest

from diy.agent_loop import Limits, Tracer, Usage, run_agent
from diy.sandbox import ExecResult, Sandbox, SandboxError
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


class FakeClient:
    """Plays back scripted responses and remembers what it was sent."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
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


def go(client, dispatcher=None, limits=None, tracer=None):
    return run_agent(client, model=MODEL, system="sys", task="do it", dispatcher=dispatcher or FakeDispatcher(),
                     limits=limits or Limits(), tracer=tracer or Tracer(None), log=lambda *_: None)


# --- loop ------------------------------------------------------------------

def test_tool_round_trip_then_completion():
    client = FakeClient(reply(text("working"), tool_use("t1", code="print(1)")),
                        reply(text("all done"), stop_reason="end_turn"))
    result = go(client)
    assert result.status == "completed" and result.final_text == "all done" and result.iterations == 2
    second_call = client.calls[1]["messages"]
    assert [m["role"] for m in second_call] == ["user", "assistant", "user"]
    assert second_call[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ran execute_python", "is_error": False}]
    assert client.calls[0]["cache_control"] == {"type": "ephemeral"}


def test_multiple_tool_calls_return_in_one_user_message():
    client = FakeClient(reply(tool_use("a", code="1"), tool_use("b", "list_files")),
                        reply(text("ok"), stop_reason="end_turn"))
    go(client)
    results = client.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert len(client.calls[1]["messages"]) == 3


def test_tool_errors_are_given_back_to_the_model():
    client = FakeClient(reply(tool_use("t1", code="boom")), reply(text("fixed"), stop_reason="end_turn"))
    result = go(client, FakeDispatcher(lambda n, a: ("Traceback ... SyntaxError", True)))
    assert result.status == "completed"
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_iteration_limit():
    client = FakeClient(reply(tool_use("t", code="1")))
    result = go(client, limits=Limits(max_iterations=3))
    assert result.status == "max_iterations" and len(client.calls) == 3


def test_budget_limit_stops_before_the_next_call():
    big = FakeUsage(input_tokens=1_000_000)  # $2 at Sonnet prices
    client = FakeClient(reply(tool_use("t", code="1"), usage=big))
    result = go(client, limits=Limits(max_cost_usd=1.0))
    assert result.status == "budget_exhausted" and len(client.calls) == 1
    assert result.cost_usd == pytest.approx(2.0001)  # 1M input + 10 output tokens


def test_duration_limit():
    result = go(FakeClient(reply(tool_use("t", code="1"))), limits=Limits(max_seconds=-1))
    assert result.status == "max_duration"


def test_api_failure_is_recorded_not_raised():
    err = anthropic.APIError("connection reset", request=SimpleNamespace(), body=None)
    result = go(FakeClient(err))
    assert result.status == "api_error" and "APIError" in result.error


def test_sandbox_failure_is_distinct_from_tool_failure():
    def broken(name, args):
        raise SandboxError("container gone")
    result = go(FakeClient(reply(tool_use("t", code="1"))), FakeDispatcher(broken))
    assert result.status == "sandbox_error" and "container gone" in result.error


def test_same_tool_use_id_is_never_executed_twice():
    client = FakeClient(reply(tool_use("same", code="1")), reply(tool_use("same", code="1")),
                        reply(text("done"), stop_reason="end_turn"))
    d = FakeDispatcher()
    result = go(client, d)
    assert result.status == "completed" and len(d.runs) == 1


def test_truncated_tool_call_is_not_executed():
    client = FakeClient(reply(tool_use("t", code="print("), stop_reason="max_tokens"),
                        reply(text("done"), stop_reason="end_turn"))
    d = FakeDispatcher()
    go(client, d)
    assert d.runs == []
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


def test_truncated_text_gets_a_continue_prompt():
    client = FakeClient(reply(text("partial"), stop_reason="max_tokens"), reply(text("done"), stop_reason="end_turn"))
    assert go(client).status == "completed"
    assert "output limit" in client.calls[1]["messages"][-1]["content"]


def test_refusal_and_unexpected_stop():
    assert go(FakeClient(reply(stop_reason="refusal"))).status == "refusal"
    assert go(FakeClient(reply(stop_reason="stop_sequence"))).status == "unexpected_stop"


def test_usage_and_cost_accounting():
    u = Usage()
    u.add(FakeUsage(1000, 100, 200, 5000))
    assert u.model_calls == 1
    assert u.cost_usd(MODEL) == pytest.approx((1000 * 2 + 100 * 10 + 200 * 2.5 + 5000 * 0.2) / 1e6)


def test_trace_is_jsonl_and_redacts_secrets(tmp_path):
    tracer = Tracer(tmp_path / "t" / "trace.jsonl")
    client = FakeClient(reply(tool_use("t", code="key = 'sk-ant-api03-SECRETVALUE_123'")),
                        reply(text("done"), stop_reason="end_turn"))
    go(client, FakeDispatcher(lambda n, a: ("sk-ant-abc123 leaked", False)), tracer=tracer)
    raw = (tmp_path / "t" / "trace.jsonl").read_text()
    events = [json.loads(line) for line in raw.splitlines()]
    assert [e["event"] for e in events] == ["run_start", "model_response", "tool_call", "model_response", "run_end"]
    assert "SECRETVALUE" not in raw and "sk-ant-abc123" not in raw and "[REDACTED]" in raw


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
        text_, is_error = d.run(name, args)
        assert is_error, (name, args)
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


def test_execute_python_stages_a_numbered_script():
    sb = FakeSandbox()
    d = Dispatcher(sb)
    d.run("execute_python", {"code": "print(1)"})
    d.run("execute_python", {"code": "print(2)", "timeout_seconds": 5})
    runs = [c for c in sb.calls if c[0][0] == "python"]
    assert runs[0][0][1].endswith("step_001.py") and runs[1][0][1].endswith("step_002.py")
    assert runs[1][2] == 5


# --- real sandbox (needs Docker) ------------------------------------------

def docker_available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


needs_docker = pytest.mark.skipif(not docker_available(), reason="Docker is not available")


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    base = tmp_path_factory.mktemp("sbx")
    data = base / "data.csv"
    data.write_text("a,b\n1,2\n3,4\n")
    with Sandbox(data, base / "workspace") as sb:
        yield sb


def py(sb, code, timeout=30):
    return sb.exec(["python", "-c", code], timeout=timeout)


@needs_docker
def test_sandbox_can_use_pandas_and_the_data(sandbox):
    r = py(sandbox, "import pandas as pd; print(pd.read_csv('/data/data.csv').a.sum())")
    assert r.exit_code == 0 and r.stdout.strip() == "4"


@needs_docker
def test_sandbox_has_no_network(sandbox):
    r = py(sandbox, "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)")
    assert r.exit_code != 0


@needs_docker
def test_sandbox_isolation_properties(sandbox):
    assert py(sandbox, "import os; print(os.getuid())").stdout.strip() == "1000"
    assert "ANTHROPIC" not in py(sandbox, "import os; print(sorted(os.environ))").stdout
    assert py(sandbox, "open('/etc/pwned', 'w')").exit_code != 0          # read-only root filesystem
    assert py(sandbox, "open('/data/data.csv', 'a').write('x')").exit_code != 0  # read-only data mount
    assert py(sandbox, "open('/workspace/ok.txt', 'w').write('x')").exit_code == 0
    assert py(sandbox, "import os; os.setuid(0)").exit_code != 0


@needs_docker
def test_sandbox_enforces_the_timeout(sandbox):
    r = py(sandbox, "import time; time.sleep(60)", timeout=2)
    assert r.timed_out and r.duration_s < 10
    assert py(sandbox, "print('still alive')").stdout.strip() == "still alive"


@needs_docker
def test_dispatcher_end_to_end_in_the_real_sandbox(sandbox):
    d = Dispatcher(sandbox)
    out, err = d.run("execute_python", {"code": "x = 1/0"})
    assert err and "ZeroDivisionError" in out and "step_001.py" in out  # traceback names the staged file
    assert d.run("write_file", {"path": "charts/c.txt", "content": "hello"}) == ("wrote 5 characters to /workspace/charts/c.txt", False)
    assert d.run("read_file", {"path": "charts/c.txt"}) == ("hello", False)
    assert d.run("read_file", {"path": "/workspace/missing.txt"})[1]
    assert "data.csv" in d.run("list_files", {"path": "/data"})[0]
