"""Offline tests for the Managed Agents client code. A fake client stands in for the API; nothing is billed."""

import ast
import json
from types import SimpleNamespace

import anthropic
import pytest

import managed.main as mm
from tests.conftest import ROOT


class Ev:
    def __init__(self, type, id, **fields):
        self.type, self.id = type, id
        self.__dict__.update(fields)

    def model_dump(self, **_):
        return {k: v for k, v in vars(self).items() if not callable(v)}


def idle(id, reason="end_turn"):
    return Ev("session.status_idle", id, stop_reason=SimpleNamespace(type=reason))


def conn_error():
    return anthropic.APIConnectionError(request=SimpleNamespace())


class FakeStream:
    def __init__(self, events, then_raise=None):
        self.events, self.then_raise = events, then_raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield from self.events
        if self.then_raise:
            raise self.then_raise


class FakeEvents:
    """events.stream plays the scripted streams in turn; events.list returns what the 'server' stored so far."""

    def __init__(self, streams, stored):
        self.streams, self.stored, self.sent, self.opened = list(streams), stored, [], 0

    def stream(self, session_id):
        self.opened += 1
        return self.streams.pop(0)

    def list(self, session_id, order="asc"):
        return list(self.stored)

    def send(self, session_id, events):
        self.sent.append(events)


def fake_client(streams, stored=()):
    events = FakeEvents(streams, list(stored))
    return SimpleNamespace(beta=SimpleNamespace(sessions=SimpleNamespace(events=events))), events


# --- the brief's constraints on this implementation -----------------------------

def test_agent_definition_matches_the_shared_prompt_and_the_diy_model():
    agent = mm.read_agent_file(mm.HERE / "agent.md")
    assert agent["system"] == (ROOT / "task" / "system.md").read_text(encoding="utf-8").strip()
    from diy.main import DEFAULT_MODEL
    assert agent["model"] == DEFAULT_MODEL
    toolset = agent["tools"][0]
    assert toolset["type"] == "agent_toolset_20260401"
    assert {c["name"]: c["enabled"] for c in toolset["configs"]} == {"web_fetch": False, "web_search": False}


def test_environment_has_no_outbound_access():
    import yaml
    config = yaml.safe_load((mm.HERE / "environment.yaml").read_text(encoding="utf-8"))["config"]
    assert config["networking"] == {"type": "limited"}
    assert "packages" not in config


def test_managed_code_has_no_local_loop_or_sandbox():
    imported = set()
    for node in ast.walk(ast.parse((mm.HERE / "main.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {n.name.split(".")[0] for n in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"subprocess", "docker", "diy", "langchain", "langgraph", "crewai", "autogen"}


# --- agent and environment reuse -----------------------------------------------

class FakeResources:
    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(
            agents=SimpleNamespace(create=self._make("agents.create", "agent_1", 1),
                                   update=self._make("agents.update", "agent_1", 2)),
            environments=SimpleNamespace(create=self._make("environments.create", "env_1", None)))

    def _make(self, name, id, version):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return SimpleNamespace(id=id, version=version)
        return call


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "STATE_FILE", tmp_path / "state.json")
    return tmp_path / "state.json"


def test_resources_are_created_once_and_then_reused(state_file):
    client = FakeResources()
    first = mm.ensure_resources(client)
    assert [c[0] for c in client.calls] == ["agents.create", "environments.create"]
    again = mm.ensure_resources(client)
    assert len(client.calls) == 2 and again == first and json.loads(state_file.read_text()) == first


def test_editing_agent_md_creates_a_new_agent_version(state_file, monkeypatch):
    client = FakeResources()
    mm.ensure_resources(client)
    real = mm.read_agent_file
    monkeypatch.setattr(mm, "read_agent_file", lambda p: {**real(p), "system": "changed"})
    state = mm.ensure_resources(client)
    name, args, kwargs = client.calls[-1]
    assert name == "agents.update" and args == ("agent_1",) and kwargs["version"] == 1
    assert state["agent"]["version"] == 2


def test_editing_the_environment_is_refused_rather_than_silently_ignored(state_file, monkeypatch):
    mm.ensure_resources(FakeResources())
    monkeypatch.setattr(mm.yaml, "safe_load", lambda text: {"name": "x", "config": {"type": "other"}}
                        if "cloud" in text else mm.yaml.__dict__["load"](text, Loader=mm.yaml.SafeLoader))
    with pytest.raises(SystemExit, match="not versioned"):
        mm.ensure_resources(FakeResources())


# --- watching a session -------------------------------------------------------

def test_watch_yields_each_event_once_across_a_dropped_stream(monkeypatch):
    monkeypatch.setattr(mm.time, "sleep", lambda s: None)
    e1, e2, e3, e4 = Ev("a", "1"), Ev("b", "2"), Ev("c", "3"), idle("4")
    # first stream delivers e1, e2 then drops; e3 happens during the gap; the second stream delivers e4
    client, events = fake_client([FakeStream([e1, e2], then_raise=conn_error()), FakeStream([e4])],
                                 stored=[e1, e2, e3])
    got = []
    for e in mm.watch(client, "s", log=lambda *_: None):
        got.append(e.id)
        if e.type == "session.status_idle":
            break
    assert got == ["1", "2", "3", "4"] and events.opened == 2


def test_watch_gives_up_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(mm.time, "sleep", lambda s: None)
    client, _ = fake_client([FakeStream([], then_raise=conn_error()) for _ in range(10)])
    with pytest.raises(anthropic.APIConnectionError):
        list(mm.watch(client, "s", log=lambda *_: None))


def test_a_session_that_has_already_finished_is_picked_up_from_the_stored_events():
    """Re-attaching after a client crash: the idle event is in the list even though the stream shows nothing new."""
    client, _ = fake_client([FakeStream([])], stored=[Ev("agent.message", "1", content=[]), idle("2")])
    got = []
    for e in mm.watch(client, "s"):
        got.append(e.id)
        if e.type == "session.status_idle":
            break
    assert got == ["1", "2"]


# --- interpreting events ---------------------------------------------------------

def test_run_state_counts_and_detects_the_end():
    state, log = mm.RunState(), lambda *_: None
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=1, cache_read_input_tokens=2)
    assert not state.handle(Ev("agent.tool_use", "1", name="bash"), log)
    assert not state.handle(Ev("span.model_request_end", "2", model_usage=usage), log)
    assert not state.handle(Ev("agent.message", "3", content=[SimpleNamespace(type="text", text="hello")]), log)
    assert not state.handle(idle("4", "requires_action"), log)  # waiting on us: keep watching
    assert state.handle(idle("5", "end_turn"), log)
    assert (state.tool_calls, state.model_calls, state.final_text, state.stop) == (1, 1, "hello", "end_turn")
    assert state.model_usage["output_tokens"] == 5


def test_terminated_and_error_events():
    state = mm.RunState()
    state.handle(Ev("session.error", "1", message="boom"), lambda *_: None)
    assert state.errors and state.handle(Ev("session.status_terminated", "2"), lambda *_: None)
    assert state.stop == "terminated"


def test_observe_interrupts_when_the_deadline_passes(tmp_path):
    client, events = fake_client([FakeStream([Ev("agent.tool_use", "1", name="bash"), Ev("agent.tool_use", "2", name="bash")])])
    state = mm.observe(client, "s", tmp_path / "t.jsonl", deadline=0, log=lambda *_: None)
    assert state.stop == "timeout" and events.sent == [[{"type": "user.interrupt"}]]
    assert len((tmp_path / "t.jsonl").read_text().splitlines()) == 1


def test_trace_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SUPERSECRETVALUE-1234567")
    client, _ = fake_client([FakeStream([Ev("agent.message", "1", content=[], note="sk-ant-api03-SUPERSECRETVALUE-1234567"),
                                         idle("2")])])
    mm.observe(client, "s", tmp_path / "t.jsonl", deadline=float("inf"), log=lambda *_: None)
    assert "SUPERSECRET" not in (tmp_path / "t.jsonl").read_text()


# --- outputs and metrics -------------------------------------------------------

class FakeFiles:
    def __init__(self, names):
        self.names = names
        self.beta = SimpleNamespace(files=self)

    def list(self, scope_id, betas):
        files = [SimpleNamespace(id=f"f{i}", filename=n, downloadable=True) for i, n in enumerate(self.names)]
        return files + [SimpleNamespace(id="input", filename="sales_transactions.csv", downloadable=False)]

    def download(self, file_id):
        def write_to_file(path):
            path.write_text(f"content of {file_id}")
        return SimpleNamespace(write_to_file=write_to_file)


def test_downloads_skip_the_input_file_rebuild_charts_and_refuse_unsafe_names(tmp_path, monkeypatch):
    # Found in the first real run: the listing returns bare file names (directories are dropped) and also
    # contains the uploaded input, which cannot be downloaded.
    monkeypatch.setattr(mm.time, "sleep", lambda s: None)
    names = ["report.md", "01_trend.png", "/mnt/session/outputs/charts/b.jpg", "../escape.txt", "summary.json"]
    saved = mm.download_outputs(FakeFiles(names), "s", tmp_path / "out", log=lambda *_: None)
    assert saved == ["report.md", "charts/01_trend.png", "charts/b.jpg", "summary.json"]
    assert (tmp_path / "out" / "charts" / "01_trend.png").is_file()
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "out" / "sales_transactions.csv").exists()


def test_metrics_mark_missing_values_as_unavailable_not_zero():
    run = mm.RunState()
    partial = SimpleNamespace(input_tokens=1_000_000, output_tokens=None, cache_creation=None,
                              cache_read_input_tokens=0, active_seconds=None)
    m = mm.metrics(SimpleNamespace(usage=partial), run, "claude-sonnet-5-5")
    assert m["estimated_token_cost_usd"] is None and m["estimated_runtime_cost_usd"] is None
    assert m["list_cost_usd_reported"] is None


def test_metrics_cost_arithmetic():
    usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=5000, active_seconds=3600,
                            cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=200, ephemeral_1h_input_tokens=50),
                            list_cost=SimpleNamespace(amount="41", currency="USD"))
    session = SimpleNamespace(usage=usage)
    m = mm.metrics(session, mm.RunState(), "claude-sonnet-5-5")
    expected = (1000 * 2 + 100 * 10 + 200 * 2.5 + 50 * 4 + 5000 * 0.1) / 1e6  # 1-hour cache writes cost 2x input
    assert m["estimated_token_cost_usd"] == pytest.approx(expected, abs=1e-4)
    assert m["estimated_runtime_cost_usd"] == 0.08 and m["list_cost_usd_reported"] == "0.41"


def test_reproduces_the_platform_reported_cost_of_the_first_real_run():
    """Real numbers from managed run-001: the platform reported a list cost of $0.60."""
    usage = SimpleNamespace(input_tokens=50, output_tokens=34022, cache_read_input_tokens=807914, active_seconds=294.374,
                            cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=67792, ephemeral_1h_input_tokens=0),
                            list_cost=SimpleNamespace(amount="60", currency="USD"))
    m = mm.metrics(SimpleNamespace(usage=usage), mm.RunState(), "claude-sonnet-5-5")
    total = m["estimated_token_cost_usd"] + m["estimated_runtime_cost_usd"]
    assert round(total, 2) == 0.60


@pytest.mark.parametrize("stop,expected", [("end_turn", "completed"), ("budget_reached", "budget_exhausted"),
                                           ("timeout", "max_duration"), ("terminated", "failed"), (None, "failed")])
def test_final_status_mapping(tmp_path, monkeypatch, stop, expected):
    monkeypatch.setattr(mm.time, "sleep", lambda s: None)
    client = FakeFiles([])
    client.beta.sessions = SimpleNamespace(retrieve=lambda sid: SimpleNamespace(
        status="idle", usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation=None,
                                             cache_read_input_tokens=0, active_seconds=1, list_cost=None)))
    run = mm.RunState()
    run.stop = stop
    code = mm.finish(client, "s", tmp_path, run, "claude-sonnet-5-5", started=0, log=lambda *_: None)
    assert json.loads((tmp_path / "run.json").read_text())["status"] == expected
    assert (code == 0) == (expected == "completed")
    assert (tmp_path / "summary.md").is_file()


def test_reattaching_does_not_duplicate_events_in_the_trace(tmp_path):
    """Found in the kill-and-resume scenario: a re-attach re-lists every stored event, so appending repeated the first ones."""
    trace = tmp_path / "t.jsonl"
    stored = [Ev("agent.tool_use", "1", name="bash"), Ev("agent.tool_use", "2", name="bash"), idle("3")]
    trace.write_text('{"id": "1"}\n{"id": "2"}\n')  # what the first, killed, client had written
    client, _ = fake_client([FakeStream([])], stored=stored)
    mm.observe(client, "s", trace, deadline=float("inf"), log=lambda *_: None, mode="w")
    ids = [json.loads(line)["id"] for line in trace.read_text().splitlines()]
    assert ids == ["1", "2", "3"]
