#!/usr/bin/env python3
"""Bridge between Claude (Desktop / Cowork) and a Brevet workspace.

Treats the chat assistant as the governed agent: Claude drafts, the human
ships a final, and this tool records the pair as Brevet evidence. Signed
releases are then materialised into one governed rules file that future
sessions load, and recalls remove them.

Identities are read from the workspace's ``agent.yaml``
(``identity_policy.owner`` and ``identity_policy.mission_group``), so this
file contains nothing personal. Authority decisions (dawn, release,
recall) always require an explicit ``--approver`` / ``--issued-by``: the
runtime rejects machine identities, and this tool never defaults them.

Subcommands
  record   log one task: Claude's draft + the human's shipped final
  dream    mine recorded overrides into candidate capabilities
  pending  list candidates awaiting a dawn decision
  dawn     apply one dawn decision (requires --approver)
  release  gate, lock, sign the next version (requires --approver)
  recall   withdraw a capability with proof (requires --issued-by)
  apply    regenerate governed/ACTIVE_CAPABILITIES.md from the signed
           release, excluding anything recalled since
  check    verify the evidence chain and that the governed file is in
           sync with the signed state
  verify   replay the evidence chain only

The workspace root defaults to the parent of this tools/ directory.
Run with the interpreter that has brevet installed (for the standard
setup: ``~/.brevet/venv/bin/python``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
GOVERNED_KINDS = {"prompt_rule", "skill", "escalation_rule", "loop_policy"}


def _identities(root: Path) -> tuple[str, str]:
    """Owner and mission group from the workspace manifest."""
    try:
        import yaml
        ident = (yaml.safe_load((root / "agent.yaml").read_text())
                 or {}).get("identity_policy", {})
        return (ident.get("owner", "human:unknown"),
                ident.get("mission_group", "mission_group:review_board"))
    except (OSError, ValueError):
        return "human:unknown", "mission_group:review_board"


def _agent(root: Path, draft: str = ""):
    os.chdir(root)
    import brevet
    return brevet.wrap(lambda task: draft)


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------- record
def cmd_record(a) -> None:
    root = Path(a.root)
    draft = Path(a.draft_file).read_text()
    final = Path(a.final_file).read_text() if a.final_file else draft
    participant = a.participant or _identities(root)[0]
    agent = _agent(root, draft)
    r = agent.run(a.task, task_family=a.family)
    ov = agent.record_final(
        r.task_id, final, participant=participant,
        rationale=a.rationale, tags=[t for t in a.tags.split(",") if t])
    _emit({"task_id": r.task_id, "family": a.family,
           "participant": participant,
           "override_harvested": ov is not None,
           "intent_preserved": getattr(ov, "intent_preserved", None),
           "accepted_verbatim": ov is None})


# ------------------------------------------------------------- loop ops
def cmd_dream(a) -> None:
    _emit(_agent(Path(a.root)).dream())


def cmd_pending(a) -> None:
    caps = _agent(Path(a.root)).dawn()
    _emit([{"capability_id": c.capability_id, "kind": c.kind.value,
            "title": c.title, "recurrence": c.evidence.recurrence_count,
            "layer": c.authority_layer.value} for c in caps])


def cmd_dawn(a) -> None:
    cap = _agent(Path(a.root)).dawn(
        decide=(a.cap, a.outcome), approver=a.approver,
        to_layer=a.to_layer, notes=a.notes)
    _emit({"capability_id": cap.capability_id,
           "outcome": a.outcome, "layer": cap.authority_layer.value,
           "validation": cap.validation_state.value, "approver": a.approver})


def cmd_release(a) -> None:
    rec = _agent(Path(a.root)).release(
        to_version=a.version, channel=a.channel, approver=a.approver,
        delta_in=a.delta_in, delta_out=a.delta_out, rationale=a.rationale)
    _emit({"from": rec.from_version, "to": rec.to_version,
           "channel": rec.channel.value, "approver": a.approver})


def cmd_recall(a) -> None:
    n = _agent(Path(a.root)).recall(
        a.cap, reason=a.reason, issued_by=a.issued_by)
    _emit({"recall_id": n.recall_id, "capability_id": n.capability_id,
           "affected_releases": [getattr(r, "version", None) or r.get("version")
                                 for r in n.affected_releases]})


def cmd_verify(a) -> None:
    ok, n = _agent(Path(a.root)).verify()
    _emit({"chain_ok": ok, "envelopes": n})


# ---------------------------------------------------------- apply/check
def _governed_text(root: Path) -> tuple[str, int]:
    """Deterministic content of the governed rules file: everything in the
    latest signed lock that is still active (recalls take effect here)."""
    from brevet.lifecycle import CapabilityStore
    lockpath = root / ".brevet" / "capabilities.lock"
    head = ["# ACTIVE CAPABILITIES (governed by Brevet)",
            "",
            "Managed by `tools/brevet_cowork.py apply`. Never hand-edit:",
            "content here exists only because a named human promoted it and",
            "a signed release shipped it. Recalled items are removed.",
            ""]
    if not lockpath.exists():
        return "\n".join(head + ["No signed release yet: no active capabilities.", ""]), 0
    lock = json.loads(lockpath.read_text())
    store = CapabilityStore(root / ".brevet" / "capabilities.jsonl").all()
    head += [f"Agent: {lock['agent']}  Version: {lock['agent_version']}",
             f"Lockfile hash: {lock['lockfile_hash']}", ""]
    body, n = [], 0
    for entry in lock.get("resolved", []):
        cap = store.get(entry["capability_id"])
        if cap is None or cap.revocation_status.value != "active":
            continue  # recalled or withdrawn since the release
        if entry["kind"] not in GOVERNED_KINDS:
            continue  # eval cases etc. stay in the workspace only
        n += 1
        body += [f"## {entry['capability_id']}",
                 f"- kind: {entry['kind']}  |  authority: {entry['authority_layer']}",
                 f"- approved_by: {entry.get('approved_by', 'unrecorded')}",
                 f"- content_hash: {entry['content_hash']}",
                 "", cap.content.strip(), ""]
    if n == 0:
        body = ["No active released capabilities (everything recalled or none released).", ""]
    return "\n".join(head + body), n


def cmd_apply(a) -> None:
    root = Path(a.root)
    text, n = _governed_text(root)
    target = root / "governed" / "ACTIVE_CAPABILITIES.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _emit({"written": str(target), "active_capabilities": n})


def cmd_check(a) -> None:
    root = Path(a.root)
    ok, n = _agent(root).verify()
    expected, count = _governed_text(root)
    target = root / "governed" / "ACTIVE_CAPABILITIES.md"
    state = ("missing" if not target.exists()
             else "in_sync" if target.read_text() == expected else "STALE")
    _emit({"chain_ok": ok, "envelopes": n,
           "governed_file": state, "active_capabilities": count})
    sys.exit(0 if ok and state == "in_sync" else 1)


# ------------------------------------------------------------------ cli
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=str(DEFAULT_ROOT),
                   help="Brevet workspace root (default: this example's folder)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="log Claude draft + human final")
    r.add_argument("--task", required=True)
    r.add_argument("--family", required=True)
    r.add_argument("--draft-file", required=True)
    r.add_argument("--final-file", help="omit if accepted verbatim")
    r.add_argument("--rationale", default="")
    r.add_argument("--tags", default="")
    r.add_argument("--participant", default=None,
                   help="defaults to identity_policy.owner from agent.yaml")
    r.set_defaults(fn=cmd_record)

    sub.add_parser("dream", help="mine overrides").set_defaults(fn=cmd_dream)
    sub.add_parser("pending", help="dawn queue").set_defaults(fn=cmd_pending)

    d = sub.add_parser("dawn", help="one dawn decision")
    d.add_argument("--cap", required=True)
    d.add_argument("--outcome", required=True,
                   choices=["promote", "hold", "reject", "re_elicit"])
    d.add_argument("--approver", required=True,
                   help="human:<who> or mission_group:<which>; never defaulted")
    d.add_argument("--to-layer", default="advisory")
    d.add_argument("--notes", default="")
    d.set_defaults(fn=cmd_dawn)

    rl = sub.add_parser("release", help="lock + sign next version")
    rl.add_argument("--version", required=True)
    rl.add_argument("--channel", default="trial")
    rl.add_argument("--approver", required=True)
    rl.add_argument("--delta-in", type=float, default=0.0)
    rl.add_argument("--delta-out", type=float, default=0.0)
    rl.add_argument("--rationale", default="")
    rl.set_defaults(fn=cmd_release)

    rc = sub.add_parser("recall", help="withdraw with proof")
    rc.add_argument("--cap", required=True)
    rc.add_argument("--reason", required=True)
    rc.add_argument("--issued-by", required=True)
    rc.set_defaults(fn=cmd_recall)

    sub.add_parser("apply", help="materialise release into governed file"
                   ).set_defaults(fn=cmd_apply)
    sub.add_parser("check", help="chain + governed-file integrity"
                   ).set_defaults(fn=cmd_check)
    sub.add_parser("verify", help="replay the chain").set_defaults(fn=cmd_verify)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
