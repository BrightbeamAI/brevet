"""Brevet: change control for what AI agents learn.

Wrap the agent you already have; get evidence, governed evolution, signed
releases, and recall. The doctrine in one sentence:

    Agents propose deltas; evidence tests them; humans promote them;
    the runtime only ever executes signed versions.

Quickstart (any framework: LangGraph, Claude Agent SDK, DeepAgents, AutoGen,
LlamaIndex, Pydantic AI, Google ADK, CrewAI, OpenAI Agents, or a callable):

    import brevet

    agent = brevet.wrap(my_agent)                        # zero config
    r = agent.run("triage DEV-4021", task_family="deviation_triage")
    agent.record_final(r.task_id, edited, participant="human:me@org")

    agent.dream()                                        # mine + compile evals
    agent.dawn(decide=(cap_id, "promote"), approver="human:me@org")
    agent.release(to_version="0.2.0", channel="trial", approver="human:me@org",
                  delta_in=0.2, delta_out=0.1)
    agent.recall(cap_id, reason="proved wrong", issued_by="human:me@org")
    agent.verify()

Custom frameworks bind in one line:

    @brevet.register_adapter("myfw", prefixes=("myfw",))
    class MyAdapter(brevet.BaseAdapter):
        def invoke(self, task, context): ...

MCP: ``brevet mcp`` exposes the full lifecycle to any MCP client.
"""

from brevet.adapters import BaseAdapter, register_adapter
from brevet.assist import ModelAssist, NoModelAssist, OllamaAssist
from brevet.models import (
    AgentManifest,
    AuthorityLayer,
    CapabilitiesLock,
    CapabilityKind,
    CapabilityObject,
    OverrideRecord,
    RecallNotice,
    ReleaseRecord,
    ValidationState,
)
from brevet.shell import BrevetAgent, BrevetShell, RunResult, wrap

__version__ = "0.1.0"

__all__ = [
    "AgentManifest",
    "AuthorityLayer",
    "BaseAdapter",
    "CapabilitiesLock",
    "CapabilityKind",
    "CapabilityObject",
    "ModelAssist",
    "NoModelAssist",
    "OllamaAssist",
    "OverrideRecord",
    "BrevetAgent",
    "BrevetShell",
    "RecallNotice",
    "ReleaseRecord",
    "RunResult",
    "ValidationState",
    "register_adapter",
    "wrap",
    "__version__",
]
