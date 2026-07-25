"""Adapters: auto-detection, seam mapping, and custom registration,
exercised against duck-typed fakes so no framework needs to be installed."""

from types import SimpleNamespace

import pytest

import brevet
from brevet.adapters import AdapterError, _text_of, detect, get_adapter

# ------------------------------------------------------------------ fakes

class FakeLangGraphApp:
    def stream(self, payload, stream_mode="values"):
        yield {"messages": [SimpleNamespace(content="thinking")]}
        yield {"messages": [SimpleNamespace(content="lg-answer")]}
    def invoke(self, payload):
        return {"messages": [SimpleNamespace(content="lg-answer")]}
FakeLangGraphApp.__module__ = "langgraph.graph.state"


class FakeDeepAgent:
    def invoke(self, payload):
        return {"messages": [SimpleNamespace(content="da-answer")]}
FakeDeepAgent.__module__ = "deepagents.graph"


class FakeAutoGenTeam:
    async def run(self, task):
        return SimpleNamespace(messages=[SimpleNamespace(source="assistant",
                                                         content="ag-answer")])
FakeAutoGenTeam.__module__ = "autogen_agentchat.agents"


class FakeLlamaAgent:
    def chat(self, task):
        return SimpleNamespace(response="li-answer")
FakeLlamaAgent.__module__ = "llama_index.core.agent"


class FakePydanticAgent:
    def run_sync(self, task):
        return SimpleNamespace(output="pa-answer", usage=lambda: "u")
FakePydanticAgent.__module__ = "pydantic_ai.agent"


class FakeADKRunner:
    def run(self, user_id=None, session_id=None, new_message=None):
        part = SimpleNamespace(text="adk-answer")
        yield SimpleNamespace(content=SimpleNamespace(parts=[part]))
FakeADKRunner.__module__ = "google.adk.runners"


class FakeCrew:
    def kickoff(self, inputs=None):
        return SimpleNamespace(raw="crew-answer")
FakeCrew.__module__ = "crewai.crew"


CASES = [
    (FakeLangGraphApp(), "langgraph", "lg-answer"),
    (FakeDeepAgent(), "deepagents", "da-answer"),
    (FakeAutoGenTeam(), "autogen", "ag-answer"),
    (FakeLlamaAgent(), "llamaindex", "li-answer"),
    (FakePydanticAgent(), "pydantic_ai", "pa-answer"),
    (FakeADKRunner(), "google_adk", "adk-answer"),
    (FakeCrew(), "crewai", "crew-answer"),
]


@pytest.mark.parametrize("target,expected_name,expected_out", CASES)
def test_detect_and_invoke(target, expected_name, expected_out, tmp_path):
    assert detect(target) == expected_name
    agent = brevet.wrap(target, workdir=tmp_path / ".brevet")
    assert agent.adapter.name == expected_name
    result = agent.run("hello")
    assert expected_out in result.output


def test_callable_fallback(tmp_path):
    agent = brevet.wrap(lambda task: f"echo:{task}", workdir=tmp_path / ".brevet")
    assert agent.adapter.name == "callable"
    assert agent.run("hi").output == "echo:hi"


def test_async_callable(tmp_path):
    async def coro_agent(task):
        return f"async:{task}"
    agent = brevet.wrap(coro_agent, workdir=tmp_path / ".brevet")
    assert agent.run("x").output == "async:x"


def test_detect_unknown_object_raises():
    class Mystery:
        pass
    with pytest.raises(AdapterError):
        detect(Mystery())


def test_custom_adapter_registration(tmp_path):
    @brevet.register_adapter("fakefw", prefixes=("fakefw",))
    class FakeFWAdapter(brevet.BaseAdapter):
        def invoke(self, task, context):
            return self.target.go(task), [{"step": "go"}]

    class FWThing:
        def go(self, task):
            return f"fw:{task}"
    FWThing.__module__ = "fakefw.core"

    agent = brevet.wrap(FWThing(), workdir=tmp_path / ".brevet")
    assert agent.adapter.name == "fakefw"
    assert agent.run("y").output == "fw:y"
    assert get_adapter("fakefw") is FakeFWAdapter


def test_zero_config_creates_manifest(tmp_path):
    def triage_bot(task):
        return "ok"
    agent = brevet.wrap(triage_bot, workdir=tmp_path / ".brevet")
    assert (tmp_path / ".brevet" / "agent.yaml").exists()
    assert agent.manifest.agent == "triage_bot"
    status = agent.status()
    assert status["chain_ok"] and status["adapter"] == "callable"


def test_text_of_shapes():
    assert _text_of(SimpleNamespace(final_output="a")) == "a"
    assert _text_of({"messages": [SimpleNamespace(content="b")]}) == "b"
    assert _text_of([1, SimpleNamespace(response="c")]) == "c"
    assert _text_of("plain") == "plain"
