"""Brevet as an MCP server.

Any MCP client (Claude Code, Claude Desktop, Cursor, an agent of your own)
gets Brevet as tools: capture, the dream cycle, dawn decisions, releases,
rollback, recall, acknowledgements, anchoring and verification. Evals run
from Python, and their runs are bound to releases made here. Start with:

    brevet mcp --workdir .brevet --manifest-path agent.yaml

or register in a client config, pointing BREVET_HOME at a folder that holds
agent.yaml and .brevet/ (clients may start servers from any directory):

    {"mcpServers": {"brevet": {"command": "brevet", "args": ["mcp"],
                               "env": {"BREVET_HOME": "/path/to/workspace"}}}}

Authority invariants hold over MCP as in code: promotion, release, rollback
and recall tools require a ``human:`` or ``mission_group:`` identity and refuse
every other namespace, including agent:, model: and dream:. Signed approvals
(below) make the identity provable: an agent that can call every tool still
cannot sign.

Automatic capture is opt-in: set ``BREVET_AUTO_CAPTURE=1`` in the server's
environment, or ``runtime_safety.evidence.auto_capture: true`` in the
manifest, to instruct sessions to record corrections without being asked.

Once the workspace registers approvers (``brevet approver add``), the dawn,
release, rollback and recall tools no longer act directly: each returns a
request that a person signs in a terminal with ``brevet approve``. An agent
can ask for a decision but cannot take it.
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
from mcp.types import ToolAnnotations

from brevet import __version__
from brevet.anchor import anchor as anchor_head
from brevet.anchor import check as check_anchors
from brevet.anchor import configured as configured_anchors
from brevet.approvals import (
    PendingRequests,
    register_from_chain,
    request_promotion,
    request_recall,
    request_release,
    request_rollback,
    verify_approvals,
)
from brevet.canonical import Signer
from brevet.chap_bridge import dispatcher_from_ref
from brevet.consent import allowed as consent_allowed
from brevet.delta import dream_cycle
from brevet.evidence import harvest_override
from brevet.harness import inventory, unmatched_patterns
from brevet.ledger import Ledger
from brevet.lifecycle import (
    CapabilityStore,
    dawn_decide,
    require_identity,
)
from brevet.lifecycle import (
    recall as _recall,
)
from brevet.lifecycle import (
    release as _release,
)
from brevet.models import (
    AgentManifest,
    ApplicabilityContext,
    AuthorityLayer,
    CapabilitiesLock,
    ReleaseChannel,
    ReleaseRecord,
)
from brevet.recalls import acknowledge, affecting, recalls_on_chain
from brevet.recalls import status as recall_status
from brevet.releases import evidence, publish
from brevet.releases import rollback as do_rollback
from brevet.serving import GOVERNED_KINDS, active_rules, file_drift, load_lock, load_manifest
from brevet.workdir import ensure_workdir, resolve_workspace

__all__ = ["GOVERNED_KINDS", "build_server", "serve"]


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
    "If brevet_active or brevet_harness reports harness files that changed "
    "without a release, tell the user: the change is not under change control "
    "until it is released. If brevet_active lists recalled rules, stop applying "
    "them at once, even if they were served earlier in the session, and call "
    "brevet_acknowledge with their recall ids; confirm the recalls it lists under "
    "recalls_to_confirm the same way. "
    "Run dawn decisions, releases, rollbacks and recalls only on the user's explicit "
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
        return load_manifest(mpath)

    m0 = _manifest()
    evidence_cfg = (m0.runtime_safety.get("evidence", {}) if m0 else {}) or {}
    auto = _truthy(os.environ.get("BREVET_AUTO_CAPTURE", "")) or _truthy(
        evidence_cfg.get("auto_capture", False))
    mcp = _make_server(_BASE_INSTRUCTIONS + (_AUTO_CAPTURE if auto else ""))

    ledger_ref = evidence_cfg.get("ledger", "file:./ledger.jsonl")
    ledger_path = wd / (ledger_ref[5:] if ledger_ref.startswith("file:") else "ledger.jsonl")
    dispatcher = dispatcher_from_ref(ledger_ref, wd)  # created once, reused by every call

    def _ledger() -> Ledger:
        return Ledger(ledger_path, dispatcher=dispatcher, manifest=_manifest())

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

    def _lock() -> CapabilitiesLock | None:
        return load_lock(wd, mpath)

    def _file_drift(lock: CapabilitiesLock | None) -> list[dict[str, Any]]:
        return file_drift(lock, _manifest(), wd, mpath)

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
        identical to the draft, means the draft was accepted as it was.
        family is the task family, the kind of work (for example
        weekly_summary); tags are comma-separated, and the first one names
        the problem, so recurring corrections should reuse it. The
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
        layer, recalled capabilities, the dawn queue, signed approvals, the
        harness, anchors, open recalls and evidence-chain integrity."""
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
            "harness": _harness_summary(),
            "recalls_open": sum(not r["complete"] for r in recall_status(ledger)),
            "anchors": configured_anchors(wd, m),
            "envelopes": n, "chain_ok": ok,
        })

    def _harness_summary() -> dict[str, Any]:
        lock = _lock()
        if lock is None:
            return {"locked_components": 0, "sources": [], "file_drift": 0}
        return {"locked_components": len(lock.harness), "sources": lock.harness_sources,
                "file_drift": len(_file_drift(lock))}

    @mcp.tool(annotations=_READ_ONLY)
    @_tool
    def brevet_harness() -> str:
        """Show the harness the latest release locked (files, agent components
        and library versions, as digests) and which declared harness files
        changed since, such as an edited prompt or a new skill. A change that
        is not released is not under change control: tell the user."""
        lock = _lock()
        if lock is None:
            return json.dumps({"release": None, "components": [],
                               "note": "no release yet"})
        m = _manifest()
        declared = bool(m and (m.bindings or {}).get("harness_files"))
        return json.dumps({
            "release": lock.agent_version,
            "sources": lock.harness_sources,
            "components": [{"component_id": c.component_id, "kind": c.kind,
                            "detail": c.detail} for c in lock.harness],
            "harness_files_declared": declared,
            "unmatched_patterns": unmatched_patterns(m, mpath, workdir=wd) if m else [],
            "file_drift": _file_drift(lock),
        })

    @mcp.tool(annotations=_READ_ONLY)
    @_tool
    def brevet_active(task_family: str = "", domain: str = "", role: str = "",
                      environment: str = "", risk_class: str = "", task: str = "") -> str:
        """Return the governed rules of the latest signed release, checked
        before they are served: the evidence chain must replay intact and
        match its anchors, the lock must match the digest the chain recorded
        for that release, the release signature must verify, and each rule's
        content and conditions must match what was released. Call at session
        start and follow every rule returned, within its conditions. To
        receive only the rules for one task, pass its family, domain, role,
        environment or risk class, and its text as task (a rule's trigger
        and exclusions are looked for in it); a rule restricted to a role,
        environment or risk class is then served only when that is given.
        Recalled rules are listed under "recalled": stop applying them, then
        call brevet_acknowledge; recalls of earlier releases not yet confirmed
        are listed under "recalls_to_confirm", confirmed the same way. Eval
        cases are never served. On any failed check nothing is served."""
        context = ApplicabilityContext.of_task(
            task or None, task_family or None,
            {"domain": domain, "role": role, "environment": environment,
             "risk_class": risk_class})
        return json.dumps(active_rules(wd, mpath, ledger=_ledger(), store=_store(),
                                       context=context))

    @mcp.tool(annotations=_ADDITIVE)
    @_tool
    def brevet_acknowledge(recall_ids: list[str]) -> str:
        """Confirm that this session has stopped applying recalled rules. Call
        after brevet_active lists them under "recalled", with their recall
        ids. Each confirmation is recorded on the evidence chain as the MCP
        serving point's acknowledgement of that recall."""
        ledger = _ledger()
        m = _manifest()
        lock = _lock()
        agent = (lock.agent if lock else None) or (m.agent if m else "agent")
        known = {r.get("recall_id"): r for r in recalls_on_chain(ledger)}
        hits = {r.get("recall_id") for r in affecting(lock, list(known.values()))}
        done, unknown = [], []
        for rid in recall_ids:
            recall = known.get(rid)
            if recall is None:
                unknown.append(rid)
                continue
            how = ("the session was told to stop applying it, and brevet_active no longer "
                   "serves it" if rid in hits else "it is not in the release served here")
            acknowledge(ledger, recall, agent=agent, release=lock.agent_version if lock else None,
                        serving_point="mcp", how=how)
            done.append(rid)
        out: dict[str, Any] = {"acknowledged": done}
        if unknown:
            out["unknown"] = unknown
        return json.dumps(out)

    @mcp.tool(annotations=_ADDITIVE)
    @_tool
    def brevet_anchor() -> str:
        """Write the evidence chain's current head to the anchors configured
        for this workspace (outside it), so a chain later cut short, rewritten
        or replaced is detected. Anchoring also happens after every governing
        step; call this to anchor recent evidence as well."""
        ledger = _ledger()
        return json.dumps(anchor_head(ledger, wd, manifest=_manifest()))

    @mcp.tool(annotations=_ADDITIVE)
    @_tool
    def brevet_dream() -> str:
        """Run the dream cycle: group recurring overrides into Evidence-layer
        candidate capabilities and compile eval cases from them. Only what is
        new is added, from the overrides the manifest's consent scope allows.
        Candidates have no authority until a human or mission group promotes
        them."""
        ledger = _ledger()
        return json.dumps(dream_cycle(ledger, _store(),
                                      consent=consent_allowed(_manifest(), ledger)))

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
                       eval_before: str = "", eval_after: str = "",
                       delta_held_in: float | None = None, delta_held_out: float | None = None,
                       rationale: str = "") -> str:
        """Resolve promoted capabilities into capabilities.lock, sign the
        manifest (Ed25519, covering the lock's digest) and record the release.
        Trial and production releases need the conservative gate to pass.
        Give eval_before and eval_after (eval run references) to bind the
        release to measured runs, whose deltas are then used; otherwise the
        deltas given here are recorded as attested by the approver, which
        production releases accept only where the manifest allows it. Run only
        on the user's explicit instruction, with the identity the user gives.
        When the workspace requires signed approvals, this returns a request for
        the user to sign."""
        manifest = _required_manifest()
        harness = inventory(manifest, mpath, workdir=wd)
        if bool(eval_before) != bool(eval_after):
            raise ValueError("give both eval_before and eval_after, or neither")
        ledger, store = _ledger(), _store()
        summary = evidence(ledger, store, manifest, harness, to_version=to_version,
                           approver=approver, previous_lock=_lock(),
                           evals=(eval_before, eval_after) if eval_before else None,
                           delta_in=delta_held_in, delta_out=delta_held_out)
        if _signing_required():
            return _awaiting(request_release(
                wd, manifest, store, to_version=to_version, channel=channel,
                approver=approver, eval_summary=summary, rationale=rationale,
                manifest_path=mpath, harness=harness))
        manifest, lock, record = _release(
            manifest, store, ledger, Signer(wd / "keys" / "brevet_ed25519.pem"),
            to_version=to_version, channel=ReleaseChannel(channel), approver=approver,
            eval_summary=summary, rationale=rationale, harness=harness)
        publish(wd, manifest, lock, mpath)
        out = {"release_id": record.release_id,
               "from": record.from_version, "to": record.to_version,
               "channel": record.channel.value, "locked": len(lock.resolved),
               "harness_components": len(lock.harness), "evidence": summary["source"]}
        missing = unmatched_patterns(manifest, mpath, workdir=wd)
        if missing:
            out["warning"] = (f"bindings.harness_files patterns that match no file, so nothing "
                              f"they name is under change control: {', '.join(missing)}")
        return json.dumps(out)

    @mcp.tool(annotations=_GOVERNING)
    @_tool
    def brevet_rollback(to_version: str, approver: str, as_version: str = "",
                        channel: str = "", rationale: str = "",
                        remove_added: bool = False) -> str:
        """Return the agent to an earlier release as a new signed release: its
        manifest and harness files come back, and its capabilities minus any
        recalled or decided against since. Run only on the user's explicit
        instruction, with the identity the user gives. When the workspace
        requires signed approvals, this returns a request for the user to
        sign. Harness files added since that release block the rollback unless
        remove_added sets them aside in the workspace."""
        manifest = _required_manifest()
        if _signing_required():
            return _awaiting(request_rollback(
                wd, manifest, _store(), target=to_version, approver=approver,
                as_version=as_version or None, channel=channel or None,
                rationale=rationale, remove_added=remove_added, manifest_path=mpath))
        manifest, lock, record, plan = do_rollback(
            wd, _ledger(), _store(), Signer(wd / "keys" / "brevet_ed25519.pem"), mpath,
            manifest, target=to_version, approver=approver, as_version=as_version or None,
            channel=channel or None, rationale=rationale, remove_added=remove_added)
        return json.dumps({"release_id": record.release_id, "from": record.from_version,
                           "to": record.to_version, "restores": to_version,
                           "locked": len(lock.resolved),
                           "restored_files": plan["restored_files"],
                           "left_out": plan["dropped"]})

    @mcp.tool(annotations=_GOVERNING)
    @_tool
    def brevet_recall(capability_id: str, reason: str, issued_by: str,
                      reason_class: str = "incorrect", severity: str = "high",
                      action: str = "rollback") -> str:
        """Recall a capability: withdraw it, flag every release whose lockfile
        contains it, and append the recall notice to the evidence chain.
        reason_class is one of incorrect, unsafe, consent_withdrawn,
        superseded, stale, compliance, duplicate or other (duplicate withdraws
        one byte-identical copy and leaves the content to the copy that
        stays; every other reason withdraws the content wherever it appears);
        severity is low, medium, high
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
        """Replay the hash-linked evidence chain, check it against the heads
        anchored outside the workspace (a chain cut short, rewritten or
        replaced fails), and check every approval signature on it."""
        ledger = _ledger()
        ok, n = ledger.verify()
        approvals = verify_approvals(ledger)
        anchored = check_anchors(ledger, wd, manifest=_manifest())
        out = {"chain_ok": ok and anchored["ok"], "envelopes": n,
               "anchors": anchored, "approvals": {
                   "signing_required": approvals["signing_required"],
                   "signed": approvals["signed"],
                   "unsigned_before_signing": approvals["unsigned_before_signing"],
                   "invalid": approvals["invalid"]}}
        if approvals["signing_required"] and out["chain_ok"]:
            from brevet.approvals import identity_policy, identity_report
            try:
                policy = identity_policy(wd, mpath if mpath.exists() else None)
            except PermissionError as e:
                out["approvals"]["identities"] = f"not checked: {e}"
            else:
                if policy:
                    out["approvals"]["identities"] = identity_report(
                        register_from_chain(ledger), policy)
        return json.dumps(out)

    return mcp


def serve(workdir: str | None = None, manifest_path: str | None = None) -> None:
    build_server(workdir, manifest_path).run()
