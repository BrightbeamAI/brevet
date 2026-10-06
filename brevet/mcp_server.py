"""Brevet as an MCP server.

Any MCP client (Claude Code, Claude Desktop, Cursor, an agent of your own)
gets the full lifecycle as tools. Start with:

    brevet mcp --workdir .brevet --manifest-path agent.yaml

or register in a client config, pointing BREVET_HOME at a folder that holds
agent.yaml and .brevet/ (clients may start servers from any directory):

    {"mcpServers": {"brevet": {"command": "brevet", "args": ["mcp"],
                               "env": {"BREVET_HOME": "/path/to/workspace"}}}}

Authority invariants hold over MCP as in code: promotion, release and
recall tools require a ``human:`` or ``mission_group:`` identity and refuse
every other namespace, including agent:, model: and dream:. The check is on
the identity supplied, not on who is calling; a deployment that must stop
an agent from promoting its own candidates also needs authenticated callers.

Automatic capture is opt-in: set ``BREVET_AUTO_CAPTURE=1`` in the server's
environment, or ``runtime_safety.evidence.auto_capture: true`` in the
manifest, to instruct sessions to record corrections without being asked.

Once the workspace registers approvers (``brevet approver add``), the dawn,
release and recall tools no longer act directly: each returns a request that
a person signs in a terminal with ``brevet approve``. An agent can ask for a
decision but cannot take it.
"""

from __future__ import annotations

import functools
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

try:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError
except ImportError:  # pragma: no cover
    try:  # MCP SDK 2.x renamed FastMCP to MCPServer and moved it
        from mcp.server.mcpserver import MCPServer as _Server
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError as e:
        raise ImportError(
            "MCP support requires the 'mcp' package: pip install 'mcp>=1.10,<3'"
        ) from e
import yaml
from mcp.types import ToolAnnotations

from brevet import __version__
from brevet.approvals import (
    PendingRequests,
    register_from_chain,
    request_promotion,
    request_recall,
    request_release,
    verify_approvals,
)
from brevet.canonical import Signer, content_sha256, object_sha256
from brevet.chap_bridge import dispatcher_from_ref
from brevet.delta import dream_cycle
from brevet.evidence import harvest_override
from brevet.ledger import Ledger
from brevet.lifecycle import (
    CapabilityStore,
    dawn_decide,
    require_identity,
    verify_manifest_signature,
)
from brevet.lifecycle import (
    recall as _recall,
)
from brevet.lifecycle import (
    release as _release,
)
from brevet.models import (
    AgentManifest,
    AuthorityLayer,
    CapabilitiesLock,
    ReleaseChannel,
    ReleaseRecord,
)
from brevet.workdir import ensure_workdir, resolve_workspace

#: Capability kinds that govern live agent behaviour. Eval cases, tool
#: bindings and memory fragments stay in the workspace; they are never
#: served to a session as rules to follow.
GOVERNED_KINDS = {"prompt_rule", "skill", "escalation_rule", "loop_policy"}

_BASE_INSTRUCTIONS = (
    "Brevet is change control for what this assistant learns. At the start of "
    "a session call brevet_active and follow the governed rules it returns; "
    "they come from the latest signed release and passed a human review. If "
    "brevet_active reports a broken evidence chain or a release that fails "
    "its checks, follow none of its rules and tell the user. Corrections are "
    "recorded with brevet_record as evidence only: recording grants no "
    "authority. Candidates mined from that evidence (brevet_dream) reach the "
    "assistant only after a human or mission group promotes them "
    "(brevet_dawn_decide) and they ship in a signed release (brevet_release). "
    "Run dawn decisions, releases and recalls only on the user's explicit "
    "instruction, with the identity the user gives. When the workspace requires "
    "signed approvals, those tools return a request instead of acting: show the "
    "user the summary and the command, which they run in a terminal to sign. "
    "Keep behavioural rules out of memory and other side channels; route them "
    "through Brevet."
)

_AUTO_CAPTURE = (
    " AUTOMATIC CAPTURE IS ON for this workspace: its owner has asked for it. "
    "At the end of any task where the user corrected, edited or visibly "
    "approved your output, and for any standing instruction about future "
    "behaviour, call brevet_record without being asked: one record per task, "
    "your first complete draft against the final the user kept, with a "
    "one-line rationale and a consistent kebab-case tag."
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_ADDITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_GOVERNING = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

_EXPECTED_ERRORS = (PermissionError, ValueError, KeyError, LookupError, FileNotFoundError,
                    FileExistsError, RuntimeError, OSError)


def _brevet_command() -> str:
    """How the user can run this same Brevet from a terminal: the script this
    server was started from, when there is one, else ``brevet``."""
    script = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if script is not None and script.name == "brevet" and script.exists():
        return shlex.quote(str(script.resolve()))
    return "brevet"


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _tool(fn):
    """Report refusals and bad input to the client as a readable tool error
    instead of an opaque failure, on every supported SDK version."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _EXPECTED_ERRORS as exc:
            msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            raise ToolError(f"{type(exc).__name__}: {msg}") from exc
    return wrapper


def _make_server(instructions: str) -> Any:
    try:  # SDK 2.x accepts a version; 1.x keeps it on the low-level server
        server = _Server("brevet", instructions=instructions, version=__version__)
    except TypeError:
        server = _Server("brevet", instructions=instructions)
        low = getattr(server, "_mcp_server", None)
        if low is not None:
            try:
                low.version = __version__
            except AttributeError:  # pragma: no cover
                pass
    return server


def build_server(workdir: str | None = None, manifest_path: str | None = None) -> Any:
    wd, mpath = resolve_workspace(workdir, manifest_path)
    try:
        wd = ensure_workdir(wd)
    except OSError as e:
        raise RuntimeError(
            f"cannot create the Brevet workspace at {wd.resolve()}: {e.strerror or e}. "
            f"Set BREVET_HOME to a writable folder, or pass --workdir and "
            f"--manifest-path.") from e

    def _manifest() -> AgentManifest | None:
        if not mpath.exists():
            return None
        return AgentManifest(**yaml.safe_load(mpath.read_text(encoding="utf-8")))

    m0 = _manifest()
    evidence_cfg = (m0.runtime_safety.get("evidence", {}) if m0 else {}) or {}
    auto = _truthy(os.environ.get("BREVET_AUTO_CAPTURE", "")) or _truthy(
        evidence_cfg.get("auto_capture", False))
    mcp = _make_server(_BASE_INSTRUCTIONS + (_AUTO_CAPTURE if auto else ""))

    ledger_ref = evidence_cfg.get("ledger", "file:./ledger.jsonl")
    ledger_path = wd / (ledger_ref[5:] if ledger_ref.startswith("file:") else "ledger.jsonl")
    dispatcher = dispatcher_from_ref(ledger_ref, wd)  # created once, reused by every call

    def _ledger() -> Ledger:
        return Ledger(ledger_path, dispatcher=dispatcher)

    def _store() -> CapabilityStore:
        return CapabilityStore(wd / "capabilities.jsonl")

    def _required_manifest() -> AgentManifest:
        m = _manifest()
        if m is None:
            raise FileNotFoundError(
                f"no manifest at {mpath}; create one with 'brevet init --directory "
                f"{mpath.parent}' and set identity_policy.owner")
        return m

    def _signing_required() -> bool:
        return register_from_chain(_ledger()).enabled

    def _awaiting(req: dict[str, Any]) -> str:
        return json.dumps({
            "status": "awaiting_signature",
            "request_id": req["request_id"],
            "summary": req["summary"],
            "next": "Nothing has changed yet. Ask the user to review the request and sign "
                    "it in a terminal; it is applied once its signatures suffice.",
            "command": f"{_brevet_command()} approve {req['request_id']} "
                       f"--workdir {shlex.quote(str(wd))}",
        })

    @mcp.tool(annotations=_ADDITIVE)
    @_tool
    def brevet_record(task: str, family: str, draft: str, final: str = "",
                      rationale: str = "", tags: str = "",
                      participant: str = "") -> str:
        """Record one completed task as evidence: the agent's draft and the
        expert's final (the version actually used). An empty final, or one
        identical to the draft, means the draft was accepted as it was. The
        participant is the human who made the correction and defaults to the
        workspace owner named in the manifest. Recording creates evidence
        only; it grants no authority."""
        ledger = _ledger()
        m = _manifest()
        owner = (m.identity_policy or {}).get("owner") if m else None
        who = require_identity(participant or owner or "human:unknown",
                               role="participant")
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

    @mcp.tool(annotations=_ADDITIVE)
    @_tool
    def brevet_chap_ingest(source: str, workspace: str = "",
                           strict: bool = False) -> str:
        """Import CHAP review verdicts as evidence: overrides (diff,
        rationale and intent_preserved carried through verbatim), rejections
        (substituting judgments) and approvals (accepted as they were). CHAP is
        the capture surface; Brevet remains the learning gate. Importing
        creates evidence only and grants no authority. ``source`` is an audit
        JSONL file or folder, a coordinator SQLite ``.db`` file, or a served
        coordinator URL (these two need ``workspace``). A per-source cursor
        makes re-runs safe, so it can run daily."""
        from brevet.chap_evidence import ingest

        summary = ingest(source, workdir=wd, workspace=workspace or None,
                         strict=strict)
        return json.dumps(summary)

    @mcp.tool(annotations=_READ_ONLY)
    @_tool
    def brevet_status() -> str:
        """Agent status: version, channel, active capabilities by authority
        layer, recalled capabilities, the dawn queue and evidence-chain
        integrity."""
        store, ledger = _store(), _ledger()
        by_layer: dict[str, int] = {}
        recalled = 0
        for c in store.all().values():
            if c.revocation_status.value == "active":
                by_layer[c.authority_layer.value] = by_layer.get(c.authority_layer.value, 0) + 1
            elif c.revocation_status.value == "withdrawn":
                recalled += 1
        ok, n = ledger.verify()
        m = _manifest()
        register = register_from_chain(ledger)
        return json.dumps({
            "agent": m.agent if m else None,
            "version": m.version if m else None,
            "channel": m.release.get("channel") if m else None,
            "capabilities_by_layer": by_layer,
            "recalled": recalled,
            "pending_dawn": len(store.pending()),
            "signed_approvals": {
                "required": register.enabled,
                "approvers": len(register.active()),
                "awaiting_signatures": len(PendingRequests(wd).pending()),
            },
            "envelopes": n, "chain_ok": ok,
        })

    @mcp.tool(annotations=_READ_ONLY)
    @_tool
    def brevet_active() -> str:
        """Return the governed rules of the latest signed release, checked
        before they are served: the evidence chain must replay intact, the
        lock must match the digest the chain recorded for that release, the
        release signature must verify, and each rule's content must match its
        recorded hash. Call at session start and follow every rule returned,
        within its conditions. Recalled capabilities are excluded and eval
        cases are never served. On any failed check nothing is served."""
        ledger = _ledger()
        ok, n = ledger.verify()
        if not ok:
            return json.dumps({
                "count": 0, "active": [], "chain_ok": False,
                "error": f"the evidence chain is broken at envelope {n + 1}; follow "
                         f"no governed rules and tell the user to run brevet verify"})
        approvals = verify_approvals(ledger)
        if approvals["invalid"]:
            return json.dumps({
                "count": 0, "active": [], "chain_ok": True,
                "error": f"{len(approvals['invalid'])} decision(s) on the evidence chain carry "
                         f"invalid approval signatures; follow no governed rules and tell "
                         f"the user to run brevet verify"})
        lockpath = next((p for p in (mpath.parent / "capabilities.lock",
                                     wd / "capabilities.lock") if p.exists()), None)
        if lockpath is None:
            return json.dumps({"count": 0, "active": [], "chain_ok": True,
                               "note": "no signed release yet"})
        lock = CapabilitiesLock(**json.loads(lockpath.read_text(encoding="utf-8")))
        digest = object_sha256([r.model_dump() for r in lock.resolved])
        releases = [e["body"] for e in ledger.read("brevet.release")
                    if e["body"].get("agent") == lock.agent]
        latest = releases[-1] if releases else None

        def refuse(reason: str) -> str:
            return json.dumps({"count": 0, "active": [], "chain_ok": True,
                               "agent": lock.agent, "agent_version": lock.agent_version,
                               "error": f"{reason}; follow no governed rules and "
                                        f"tell the user"})

        if latest is None:
            return refuse("capabilities.lock has no release on the evidence chain")
        if not (digest == lock.lockfile_hash == latest.get("lockfile_hash")):
            return refuse("capabilities.lock does not match the digest recorded for "
                          f"release {latest.get('to_version')}")
        signature = "not checked: this release predates recorded signing keys"
        if latest.get("signer_public_key"):
            m = _manifest()
            if (m is None or m.version != latest.get("to_version")
                    or not verify_manifest_signature(m, latest["signer_public_key"])
                    or (m.release or {}).get("lockfile_hash") != digest):
                return refuse("the manifest signature for release "
                              f"{latest.get('to_version')} does not verify")
            signature = "verified"

        store = _store().all()
        rules, withheld = [], []
        for entry in lock.resolved:
            cap = store.get(entry.capability_id)
            if cap is None or cap.revocation_status.value != "active":
                continue  # recalled or withdrawn since the release
            if entry.kind not in GOVERNED_KINDS:
                continue
            if content_sha256(cap.content) != entry.content_hash or not cap.releasable:
                withheld.append(entry.capability_id)
                continue
            conditions = {k: v for k, v in cap.conditions.model_dump().items() if v}
            rules.append({
                "capability_id": entry.capability_id,
                "kind": entry.kind,
                "authority_layer": entry.authority_layer,
                "approved_by": entry.approved_by,
                "content_hash": entry.content_hash,
                "title": cap.title,
                "content": cap.content.strip(),
                "conditions": conditions,
            })
        out = {
            "agent": lock.agent,
            "agent_version": lock.agent_version,
            "lockfile_hash": lock.lockfile_hash,
            "chain_ok": True,
            "signature": signature,
            "signed_approvals": ("required and verified" if approvals["signing_required"]
                                 else "not required in this workspace"),
            "count": len(rules),
            "active": rules,
        }
        if withheld:
            out["withheld"] = withheld
            out["warning"] = ("these capabilities no longer match the released "
                              "content and were not served; tell the user")
        return json.dumps(out)

    @mcp.tool(annotations=_ADDITIVE)
    @_tool
    def brevet_dream() -> str:
        """Run the dream cycle: group recurring overrides into Evidence-layer
        candidate capabilities and compile eval cases from them. Only what is
        new is added. Candidates have no authority until a human promotes
        them."""
        return json.dumps(dream_cycle(_ledger(), _store()))

    @mcp.tool(annotations=_READ_ONLY)
    @_tool
    def brevet_dawn_pending() -> str:
        """List the candidates awaiting a dawn decision: id, kind, recurrence
        and title. Eval cases appear too, because their labels can be wrong."""
        return json.dumps([
            {"capability_id": c.capability_id, "kind": c.kind.value,
             "recurrence": c.evidence.recurrence_count, "title": c.title}
            for c in _store().pending()
        ])

    @mcp.tool(annotations=_GOVERNING)
    @_tool
    def brevet_dawn_decide(capability_id: str, outcome: str, approver: str,
                           to_layer: str = "advisory", notes: str = "") -> str:
        """Apply one dawn-gate decision: promote, hold, reject or re_elicit.
        The approver must be human:<who> or mission_group:<name>; promotion
        to the controlled layer needs a mission group. Run only on the user's
        explicit instruction, with the identity the user gives. When the workspace
        requires signed approvals, this returns a request for the user to sign."""
        if _signing_required():
            return _awaiting(request_promotion(wd, _store(), capability_id, outcome,
                                               approver=approver, to_layer=to_layer,
                                               notes=notes))
        cap = dawn_decide(_store(), _ledger(), capability_id, outcome,
                          approver=approver, to_layer=AuthorityLayer(to_layer), notes=notes)
        return json.dumps({"capability_id": cap.capability_id,
                           "validation_state": cap.validation_state.value,
                           "authority_layer": cap.authority_layer.value})

    @mcp.tool(annotations=_GOVERNING)
    @_tool
    def brevet_release(to_version: str, approver: str, channel: str = "shadow",
                       delta_held_in: float = 0.0, delta_held_out: float = 0.0,
                       rationale: str = "") -> str:
        """Resolve promoted capabilities into capabilities.lock, sign the
        manifest (Ed25519, covering the lock's digest) and record the release.
        Trial and production releases need the conservative gate to pass on
        the deltas supplied here (measured, or attested by the user). Run only
        on the user's explicit instruction, with the identity the user gives. When
        the workspace requires signed approvals, this returns a request for the user
        to sign."""
        if _signing_required():
            return _awaiting(request_release(
                wd, _required_manifest(), _store(), to_version=to_version, channel=channel,
                approver=approver,
                eval_summary={"delta_held_in": delta_held_in, "delta_held_out": delta_held_out,
                              "gate": "conservative"},
                rationale=rationale, manifest_path=mpath))
        manifest, lock, record = _release(
            _required_manifest(), _store(), _ledger(),
            Signer(wd / "keys" / "brevet_ed25519.pem"),
            to_version=to_version, channel=ReleaseChannel(channel), approver=approver,
            eval_summary={"delta_held_in": delta_held_in, "delta_held_out": delta_held_out,
                          "gate": "conservative"},
            rationale=rationale)
        mpath.write_text(yaml.safe_dump(manifest.model_dump(exclude_none=False),
                                        sort_keys=False), encoding="utf-8")
        (mpath.parent / "capabilities.lock").write_text(lock.model_dump_json(indent=2),
                                                         encoding="utf-8")
        return json.dumps({"release_id": record.release_id,
                           "from": record.from_version, "to": record.to_version,
                           "channel": record.channel.value, "locked": len(lock.resolved)})

    @mcp.tool(annotations=_GOVERNING)
    @_tool
    def brevet_recall(capability_id: str, reason: str, issued_by: str,
                      reason_class: str = "incorrect", severity: str = "high",
                      action: str = "rollback") -> str:
        """Recall a capability: withdraw it, flag every release whose lockfile
        contains it, and append the recall notice to the evidence chain.
        reason_class is one of incorrect, unsafe, consent_withdrawn,
        superseded, stale, compliance or other; severity is low, medium, high
        or critical; action is quarantine, rollback or re_review. Run only on
        the user's explicit instruction, with the identity the user gives. When the
        workspace requires signed approvals, this returns a request for the user to
        sign."""
        if _signing_required():
            return _awaiting(request_recall(
                wd, _store(), capability_id, reason=reason, issued_by=issued_by,
                reason_class=reason_class, severity=severity, action=action))
        ledger = _ledger()
        releases = [ReleaseRecord(**e["body"]) for e in ledger.read("brevet.release")]
        notice = _recall(_store(), ledger, capability_id, reason=reason,
                         reason_class=reason_class, severity=severity,
                         issued_by=issued_by, releases=releases, action=action)
        return json.dumps({"recall_id": notice.recall_id,
                           "affected_releases": notice.affected_releases})

    @mcp.tool(annotations=_READ_ONLY)
    @_tool
    def brevet_verify() -> str:
        """Replay the hash-linked evidence chain and report whether it is
        intact, and check every approval signature on it. Replay detects
        edits that break the chain; detecting a wholesale rewrite needs the
        latest hash held somewhere else."""
        ledger = _ledger()
        ok, n = ledger.verify()
        approvals = verify_approvals(ledger)
        return json.dumps({"chain_ok": ok, "envelopes": n, "approvals": {
            "signing_required": approvals["signing_required"],
            "signed": approvals["signed"],
            "unsigned_before_signing": approvals["unsigned_before_signing"],
            "invalid": approvals["invalid"]}})

    return mcp


def serve(workdir: str | None = None, manifest_path: str | None = None) -> None:
    build_server(workdir, manifest_path).run()
