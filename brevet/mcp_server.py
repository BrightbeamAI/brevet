"""Brevet as an MCP server.

Any MCP client (Claude Code, Claude Desktop, Cursor, an agent of your own)
gets the full lifecycle as tools. Start with:

    brevet mcp --workdir .brevet --manifest agent.yaml

or register in a client config:

    {"mcpServers": {"brevet": {"command": "brevet", "args": ["mcp"]}}}

Authority invariants hold over MCP exactly as in code: promotion tools
require an approver identity and reject agent/model/dream namespaces, so an
agent calling these tools still cannot promote its own capabilities.

Requires the optional dependency:  pip install "mcp>=1.2"
(installed automatically by the [mcp] extra when installing from the
repository).
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP  # MCP SDK 1.x
except ImportError:  # pragma: no cover
    try:
        # MCP SDK 2.0 renamed FastMCP to MCPServer and moved it; the
        # surface Brevet uses (tool decorator, stdio run) is unchanged.
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError as e:
        raise ImportError(
            "MCP support requires the 'mcp' package: pip install 'mcp>=1.2'"
        ) from e

import yaml

from brevet.canonical import Signer
from brevet.delta import load_overrides, mine
from brevet.evals import compile_suite
from brevet.evidence import harvest_override
from brevet.ledger import Ledger
from brevet.lifecycle import (
    CapabilityStore,
    dawn_decide,
)
from brevet.lifecycle import (
    recall as _recall,
)
from brevet.lifecycle import (
    release as _release,
)
from brevet.models import AgentManifest, AuthorityLayer, ReleaseChannel, ReleaseRecord


def build_server(workdir: str = ".brevet", manifest_path: str = "agent.yaml") -> FastMCP:
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    mcp = FastMCP(
        "brevet",
        instructions=(
            "Change control for what AI agents learn. Candidates are mined from "
            "evidence; only a human or mission-group approver may promote; "
            "releases are signed; recall withdraws capability provably."
        ),
    )

    def _ledger() -> Ledger:
        return Ledger(wd / "ledger.jsonl")

    def _store() -> CapabilityStore:
        return CapabilityStore(wd / "capabilities.jsonl")

    def _manifest() -> AgentManifest:
        return AgentManifest(**yaml.safe_load(Path(manifest_path).read_text()))

    @mcp.tool()
    def brevet_record(task: str, family: str, draft: str, final: str = "",
                      rationale: str = "", tags: str = "",
                      participant: str = "") -> str:
        """Record one completed task as evidence: the agent's draft and the
        human's shipped final. An empty final means accepted verbatim. The
        participant defaults to the workspace owner. Recording creates
        evidence only; it grants no authority."""
        ledger = _ledger()
        m = _manifest() if Path(manifest_path).exists() else None
        who = participant or (
            (m.identity_policy or {}).get("owner") if m else None) or "human:unknown"
        task_id = ledger.append("brevet.task", {
            "task": task, "task_family": family,
            "agent": m.agent if m else "agent",
            "agent_version": m.version if m else "0.0.0",
            "channel": (m.release.get("channel", "shadow") if m else "shadow"),
        })
        ledger.append("brevet.artefact", {
            "task_id": task_id, "output": draft, "trace_len": 0, "trace": [],
        }, refs=[task_id])
        ov = harvest_override(
            task_id=task_id, draft=draft, final=final or draft,
            participant=who, rationale=rationale,
            tags=[t.strip() for t in tags.split(",") if t.strip()],
            task_family=family,
        )
        if ov is None:
            ledger.append("brevet.artefact",
                          {"task_id": task_id, "accepted_verbatim": True},
                          refs=[task_id])
            return json.dumps({"task_id": task_id, "family": family,
                               "participant": who, "accepted_verbatim": True})
        ledger.append("brevet.override", ov.model_dump(), refs=[task_id])
        return json.dumps({"task_id": task_id, "family": family,
                           "participant": who, "override_harvested": True,
                           "intent_preserved": ov.intent_preserved})

    @mcp.tool()
    def brevet_status() -> str:
        """Agent status: version, channel, capability counts by authority layer,
        pending dawn queue, evidence-chain integrity."""
        store, ledger = _store(), _ledger()
        by_layer: dict[str, int] = {}
        for c in store.all().values():
            by_layer[c.authority_layer.value] = by_layer.get(c.authority_layer.value, 0) + 1
        ok, n = ledger.verify()
        m = _manifest() if Path(manifest_path).exists() else None
        return json.dumps({
            "agent": m.agent if m else None,
            "version": m.version if m else None,
            "channel": m.release.get("channel") if m else None,
            "capabilities_by_layer": by_layer,
            "pending_dawn": len(store.pending()),
            "envelopes": n, "chain_ok": ok,
        })

    @mcp.tool()
    def brevet_dream() -> str:
        """Run the offline mining cycle: cluster overrides into Evidence-layer
        candidate capabilities and compile the override-derived eval suite.
        Candidates carry zero authority until a human promotes them."""
        ledger, store = _ledger(), _store()
        overrides = load_overrides(ledger)
        candidates = mine(overrides, ledger=ledger)
        for cand in candidates:
            store.add(cand)
            ledger.append("brevet.candidate",
                          {"capability_id": cand.capability_id, "title": cand.title})
        cases = compile_suite([o for o in overrides if not o.intent_preserved])
        for case in cases:
            store.add(case)
        return json.dumps({"overrides": len(overrides), "candidates": len(candidates),
                           "eval_cases": len(cases)})

    @mcp.tool()
    def brevet_dawn_pending() -> str:
        """List candidates awaiting human review (id, kind, recurrence, title)."""
        return json.dumps([
            {"capability_id": c.capability_id, "kind": c.kind.value,
             "recurrence": c.evidence.recurrence_count, "title": c.title}
            for c in _store().pending()
        ])

    @mcp.tool()
    def brevet_dawn_decide(capability_id: str, outcome: str, approver: str,
                           to_layer: str = "advisory", notes: str = "") -> str:
        """Apply one dawn-gate decision (promote|hold|reject|re_elicit).
        approver MUST be a human or mission-group identity; agent/model/dream
        identities are rejected by the authority invariant."""
        cap = dawn_decide(_store(), _ledger(), capability_id, outcome,
                          approver=approver, to_layer=AuthorityLayer(to_layer), notes=notes)
        return json.dumps({"capability_id": cap.capability_id,
                           "validation_state": cap.validation_state.value,
                           "authority_layer": cap.authority_layer.value})

    @mcp.tool()
    def brevet_release(to_version: str, approver: str, channel: str = "shadow",
                       delta_held_in: float = 0.0, delta_held_out: float = 0.0,
                       rationale: str = "") -> str:
        """Resolve promoted capabilities into capabilities.lock, sign the
        manifest (ed25519), and record the release. Non-shadow channels
        require the conservative eval gate to pass."""
        manifest = _manifest()
        manifest, lock, record = _release(
            manifest, _store(), _ledger(), Signer(wd / "keys" / "brevet_ed25519.pem"),
            to_version=to_version, channel=ReleaseChannel(channel), approver=approver,
            eval_summary={"delta_held_in": delta_held_in, "delta_held_out": delta_held_out,
                          "gate": "conservative"},
            rationale=rationale)
        Path(manifest_path).write_text(yaml.safe_dump(
            manifest.model_dump(exclude_none=False), sort_keys=False))
        (Path(manifest_path).parent / "capabilities.lock").write_text(
            lock.model_dump_json(indent=2))
        return json.dumps({"release_id": record.release_id,
                           "from": record.from_version, "to": record.to_version,
                           "channel": channel, "locked": len(lock.resolved)})

    @mcp.tool()
    def brevet_recall(capability_id: str, reason: str, issued_by: str,
                      reason_class: str = "incorrect", severity: str = "high") -> str:
        """Withdraw a capability: revoke it, flag every release whose lockfile
        contains it, and chain the recall notice as evidence."""
        ledger = _ledger()
        releases = [ReleaseRecord(**e["body"]) for e in ledger.read("brevet.release")]
        notice = _recall(_store(), ledger, capability_id, reason=reason,
                         reason_class=reason_class, severity=severity,
                         issued_by=issued_by, releases=releases)
        return json.dumps({"recall_id": notice.recall_id,
                           "affected_releases": notice.affected_releases})

    @mcp.tool()
    def brevet_verify() -> str:
        """Independently replay the hash-linked evidence chain."""
        ok, n = _ledger().verify()
        return json.dumps({"chain_ok": ok, "envelopes": n})

    return mcp


def serve(workdir: str = ".brevet", manifest_path: str = "agent.yaml") -> None:
    build_server(workdir, manifest_path).run()
