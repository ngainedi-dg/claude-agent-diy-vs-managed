"""Tests that run real containers. Skipped when Docker is not available."""

import shutil
import subprocess

import pytest

from diy.sandbox import Sandbox
from diy.tools import Dispatcher


def docker_available():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not docker_available(), reason="Docker is not available")


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    base = tmp_path_factory.mktemp("sbx")
    data = base / "data.csv"
    data.write_text("a,b\n1,2\n3,4\n")
    with Sandbox(data, base / "workspace") as sb:
        yield sb


def py(sb, code, timeout=30):
    return sb.exec(["python", "-c", code], timeout=timeout)


def test_sandbox_can_use_pandas_and_the_data(sandbox):
    r = py(sandbox, "import pandas as pd; print(pd.read_csv('/data/data.csv').a.sum())")
    assert r.exit_code == 0 and r.stdout.strip() == "4"


def test_sandbox_has_no_network(sandbox):
    r = py(sandbox, "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)")
    assert r.exit_code != 0


def test_sandbox_isolation_properties(sandbox):
    assert py(sandbox, "import os; print(os.getuid())").stdout.strip() == "1000"
    assert "ANTHROPIC" not in py(sandbox, "import os; print(sorted(os.environ))").stdout
    assert py(sandbox, "open('/etc/pwned', 'w')").exit_code != 0                  # read-only root filesystem
    assert py(sandbox, "open('/data/data.csv', 'a').write('x')").exit_code != 0    # read-only data mount
    assert py(sandbox, "open('/workspace/ok.txt', 'w').write('x')").exit_code == 0
    assert py(sandbox, "import os; os.setuid(0)").exit_code != 0


def test_sandbox_has_no_docker_socket_and_no_extra_mounts(sandbox):
    mounts = py(sandbox, "print(open('/proc/mounts').read())").stdout
    assert "docker.sock" not in mounts
    host_mounts = [line.split()[1] for line in mounts.splitlines() if line.strip() and line.split()[1].startswith(("/data", "/workspace"))]
    assert sorted(host_mounts) == ["/data/data.csv", "/workspace"]


def test_sandbox_limits_memory_and_processes(sandbox):
    big = py(sandbox, "x = bytearray(3 * 1024**3); print('allocated')")  # limit is 1g
    assert "allocated" not in big.stdout and big.exit_code != 0
    forks = py(sandbox, "import os\nn = 0\nfor _ in range(2000):\n    try:\n        os.fork() or os._exit(0)\n        n += 1\n"
                        "    except OSError:\n        break\nprint('forked', n)", timeout=30)
    assert forks.exit_code != 0 or int(forks.stdout.split()[-1]) < 2000  # process limit reached or killed


def test_sandbox_enforces_the_timeout(sandbox):
    r = py(sandbox, "import time; time.sleep(60)", timeout=2)
    assert r.timed_out and r.duration_s < 10
    assert py(sandbox, "print('still alive')").stdout.strip() == "still alive"


def test_sandbox_recovers_workspace_after_restart(tmp_path):
    """A fresh container on the same workspace sees the files an earlier container wrote (what resume relies on)."""
    data = tmp_path / "d.csv"
    data.write_text("a\n1\n")
    with Sandbox(data, tmp_path / "ws") as first:
        py(first, "open('/workspace/kept.txt', 'w').write('hello')")
    with Sandbox(data, tmp_path / "ws") as second:
        assert py(second, "print(open('/workspace/kept.txt').read())").stdout.strip() == "hello"


def test_dispatcher_end_to_end_in_the_real_sandbox(sandbox):
    d = Dispatcher(sandbox)
    out, err = d.run("execute_python", {"code": "x = 1/0"})
    assert err and "ZeroDivisionError" in out and "step_001.py" in out  # traceback names the staged file
    assert d.run("write_file", {"path": "charts/c.txt", "content": "hello"}) == ("wrote 5 characters to /workspace/charts/c.txt", False)
    assert d.run("read_file", {"path": "charts/c.txt"}) == ("hello", False)
    assert "data.csv" in d.run("list_files", {"path": "/data"})[0]


def test_tool_level_failures_come_back_as_errors_not_exceptions(sandbox):
    d = Dispatcher(sandbox)
    syntax, err = d.run("execute_python", {"code": "def broken(:\n    pass"})
    assert err and "SyntaxError" in syntax
    missing, err = d.run("read_file", {"path": "/workspace/nope.txt"})
    assert err and "No such file" in missing
    missing_input, err = d.run("execute_python", {"code": "open('/data/not_there.csv')"})
    assert err and "FileNotFoundError" in missing_input
    slow, err = d.run("execute_python", {"code": "import time; time.sleep(60)", "timeout_seconds": 2})
    assert err and "exceeded 2s timeout" in slow
    assert d.run("execute_python", {"code": "print('fine')"})[1] is False  # and the sandbox is still usable


def test_a_dead_container_is_reported_as_a_sandbox_failure(tmp_path):
    from diy.sandbox import SandboxError
    data = tmp_path / "d.csv"
    data.write_text("a\n1\n")
    with Sandbox(data, tmp_path / "ws") as sb:
        subprocess.run(["docker", "rm", "-f", sb.name], capture_output=True)
        with pytest.raises(SandboxError):
            py(sb, "print(1)")


def test_interrupted_run_resumes_in_a_new_container_with_the_workspace_intact(tmp_path):
    """The whole recovery path with real SQLite and real containers; only the model is scripted."""
    from diy.agent_loop import Tracer, run_agent
    from diy.sessions import Limits, Session
    from tests.test_diy import FakeClient, SimulatedCrash, reply, text, tool_use

    data = tmp_path / "d.csv"
    data.write_text("a\n1\n")
    run_dir = tmp_path / "run"
    write = tool_use("w1", "write_file", path="notes.txt", content="written before the crash")
    session = Session.create(run_dir / "session.sqlite", task="t", system="s", model="claude-sonnet-5-5", limits=Limits())

    with Sandbox(data, run_dir / "outputs") as sb, pytest.raises(SimulatedCrash):
        run_agent(FakeClient(reply(write), SimulatedCrash()), session=session, dispatcher=Dispatcher(sb),
                  tracer=Tracer(None), log=lambda *_: None)

    resumed = Session.load(run_dir / "session.sqlite")
    assert resumed.status == "running" and resumed.tool_call("w1")["status"] == "done"
    client = FakeClient(reply(tool_use("r1", "read_file", path="notes.txt")), reply(text("done"), stop_reason="end_turn"))
    with Sandbox(data, run_dir / "outputs") as sb2:  # a brand-new container
        result = run_agent(client, session=resumed, dispatcher=Dispatcher(sb2), tracer=Tracer(None), log=lambda *_: None)
    assert result.status == "completed"
    last_results = client.calls[1]["messages"][-1]["content"]
    assert last_results[0]["content"] == "written before the crash"
