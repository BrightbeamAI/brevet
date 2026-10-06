"""Brevet: change control for what AI agents learn.

    brevet demo          run the governed evolution loop once, offline
    brevet playground    step through the loop, stage by stage, in a browser
    brevet init          create a starter manifest (agent.yaml) and .brevet/
    brevet dream         mine recurring overrides into candidate capabilities
    brevet dawn          the dawn gate: list candidates, or decide one
    brevet release       pass the conservative gate, sign and release
    brevet recall        recall a capability and flag every release with it
    brevet verify        replay the evidence chain to detect edits
    brevet status        version, capabilities by authority layer, chain health
    brevet chap-ingest   import CHAP review verdicts as overrides
    brevet mcp           serve the loop to an MCP client such as Claude Desktop
    brevet approver      register the people whose signatures decisions need
    brevet approve       review and sign pending decisions
    brevet harness       compare the harness files with the latest release
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
import yaml

from brevet import __version__
from brevet.anchor import anchor as anchor_head
from brevet.anchor import check as check_anchors
from brevet.anchor import configured as configured_anchors
from brevet.approvals import (
    ApproverKey,
    PendingRequests,
    apply_if_ready,
    create_key,
    identity_policy,
    identity_problems,
    identity_report,
    key_dir,
    load_key,
    local_identities,
    register_from_chain,
    request_promotion,
    request_recall,
    request_register_add,
    request_register_revoke,
    request_release,
    request_rollback,
    request_threshold,
    sign_request,
    use_ssh_key,
    verify_approvals,
)
from brevet.canonical import Signer
from brevet.consent import record_withdrawal
from brevet.delta import dream_cycle
from brevet.harness import compare, inventory, load_lock, unmatched_patterns
from brevet.ledger import Ledger
from brevet.lifecycle import CapabilityStore, dawn_decide
from brevet.lifecycle import recall as do_recall
from brevet.lifecycle import release as do_release
from brevet.models import AgentManifest, AuthorityLayer, ReleaseChannel, ReleaseRecord
from brevet.recalls import status as recall_status
from brevet.releases import evidence, publish
from brevet.releases import rollback as do_rollback
from brevet.serving import file_drift
from brevet.workdir import ensure_workdir

app = typer.Typer(add_completion=False, help=__doc__, pretty_exceptions_enable=False)
approver_app = typer.Typer(no_args_is_help=True, help=(
    "Register the people whose signatures this workspace requires. Once the first "
    "approver is registered, every dawn decision, release and recall must be signed."))
app.add_typer(approver_app, name="approver")
consent_app = typer.Typer(no_args_is_help=True, help=(
    "Consent: stop learning from a participant who withdraws it."))
app.add_typer(consent_app, name="consent")


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _load(workdir: Path) -> tuple[Ledger, CapabilityStore, Signer]:
    return (
        Ledger(workdir / "ledger.jsonl"),
        CapabilityStore(workdir / "capabilities.jsonl"),
        Signer(workdir / "keys" / "brevet_ed25519.pem"),
    )


def _existing(workdir: str) -> Path:
    """A workspace that must already exist (reading commands never create one)."""
    wd = Path(workdir)
    if not (wd / "ledger.jsonl").exists():
        raise FileNotFoundError(f"no evidence chain at {wd / 'ledger.jsonl'}; "
                                f"is --workdir right?")
    return wd


def _signing_required(wd: Path) -> bool:
    return register_from_chain(Ledger(wd / "ledger.jsonl")).enabled


def _signer_identity(prefer: str | None = None, identity: str | None = None) -> str:
    """The local approver key to sign with."""
    local = local_identities()
    if identity:
        if identity not in local:
            raise FileNotFoundError(f"no approver key for {identity} in {key_dir()}")
        return identity
    if prefer and prefer in local:
        return prefer
    if not local:
        raise FileNotFoundError(f"no approver key in {key_dir()}; create one with "
                                f"'brevet approver add'")
    if len(local) > 1:
        raise ValueError(f"several approver keys on this machine ({', '.join(local)}); "
                         f"choose one with --identity")
    return local[0]


def _unlock(identity: str) -> ApproverKey:
    return load_key(identity, getpass.getpass(f"Passphrase for {identity}: "))


def _describe(result: dict) -> str:
    if "validation_state" in result:
        return (f"{result['capability_id']} is now {result['validation_state']} "
                f"(authority layer: {result['authority_layer']})")
    if "to" in result:
        return (f"released {result['from']} -> {result['to']} on the {result['channel']} "
                f"channel, locking {_plural(result['locked'], 'capability', 'capabilities')}")
    if "recall_id" in result:
        flagged = ", ".join(result["affected_releases"]) or "none"
        return f"recalled; releases flagged: {flagged}"
    return f"register change applied ({result.get('change')} {result.get('identity')})"


def _sign_and_apply(wd: Path, request_ids: list[str], *, identity: str | None = None,
                    prefer: str | None = None, manifest_path: str | None = None) -> None:
    key = _unlock(_signer_identity(prefer, identity))
    for rid in request_ids:
        sign_request(wd, rid, key)
        try:
            out = apply_if_ready(wd, rid, manifest_path=manifest_path)
        except (PermissionError, ValueError, KeyError, FileNotFoundError) as e:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            typer.echo(f"Could not apply {rid}: {msg}. The request is closed; ask again.")
            continue
        if out["status"] == "applied":
            typer.echo(f"Signed {rid} as {key.identity}: {_describe(out['result'])}.")
        else:
            typer.echo(f"Signed {rid} as {key.identity}; it still needs "
                       f"{'; '.join(out.get('needs', []))}.")


def _request_then_offer(wd: Path, req: dict, *, prefer: str | None = None) -> None:
    """Report a new approval request, and sign it at once if this terminal
    has a usable approver key."""
    rid = req["request_id"]
    typer.echo(f"This workspace requires signed approvals. Request {rid}: {req['summary']}")
    if sys.stdin.isatty() and local_identities() and typer.confirm("Sign it now?", default=True):
        _sign_and_apply(wd, [rid], prefer=prefer)
        return
    typer.echo(f"Nothing has changed yet. To sign it: brevet approve {rid}")


def _read_manifest(manifest_path: str) -> AgentManifest:
    path = Path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"no manifest at {path}; create one with 'brevet init'")
    return AgentManifest(**yaml.safe_load(path.read_text(encoding="utf-8")))


@app.command()
def version() -> None:
    """Show the installed Brevet version."""
    typer.echo(f"brevet {__version__}")


@app.command()
def chap_ingest(
    source: str = typer.Argument(..., help="a CHAP audit file or folder, a coordinator .db file, or a URL"),
    workdir: str = typer.Option(".brevet"),
    workspace: str = typer.Option(None, help="the CHAP workspace id; needed for .db files and URLs"),
    strict: bool = typer.Option(False, help="import nothing if the CHAP chain fails its check"),
) -> None:
    """Import CHAP review verdicts as overrides.

    CHAP is the capture surface; Brevet remains the learning gate. Imported
    overrides, approvals and rejections feed the dream cycle exactly like
    overrides recorded with record_final, and a verdict already recorded
    in-session is not counted twice."""
    from brevet.chap_evidence import ingest

    summary = ingest(source, workdir=ensure_workdir(workdir), workspace=workspace,
                     strict=strict)
    typer.echo(
        f"Imported from CHAP (chain check: {summary['chain']}): "
        f"{_plural(summary['overrides'], 'override')}, "
        f"{_plural(summary['approvals'], 'approval')}, "
        f"{_plural(summary['rejections'], 'rejection')} from "
        f"{_plural(len(summary['workspaces']), 'workspace')}; "
        f"{summary.get('duplicates_skipped', 0)} already recorded and skipped.")


@app.command()
def init(agent: str = "my_agent", directory: str = ".",
         force: bool = typer.Option(False, help="overwrite an existing agent.yaml")) -> None:
    """Create a starter manifest (agent.yaml) and the .brevet workdir."""
    out = Path(directory) / "agent.yaml"
    if out.exists() and not force:
        raise FileExistsError(f"{out} already exists; pass --force to replace it")
    manifest = AgentManifest(
        agent=agent,
        description="Scaffolded by brevet init.",
        identity_policy={"agent_id": agent, "owner": "human:owner@example.com",
                         "mission_group": None, "may": [], "may_not": []},
        prompt_architecture={"system_prompt": "You are a careful assistant.",
                             "refusal_triggers": []},
        cognitive_core={"model_policy": {"local_default": "ollama:gemma4:12b",
                                         "allowed_remote": ["claude"]}},
        bindings={"tools": {"read": [], "suggest": [], "act": [], "controlled_act": []},
                  "memory": {"procedural": True, "semantic": True, "episodic": True,
                             "tacit": None},
                  "capabilities_lock": "capabilities.lock"},
        runtime_safety={"loop": {"policy": "react", "max_iterations": 8},
                        "evidence": {"ledger": "file:./ledger.jsonl",
                                     "profiles": ["core/1.0", "review/1.0", "brevet/1.0"],
                                     "capture_overrides": True},
                        "evals": {"regression_suite": None, "gate": "conservative", "repeats": 3},
                        "dream": {"enabled": True, "schedule": "02:00",
                                  "consent_scope": "consented_sources_only"}},
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(manifest.model_dump(exclude_none=False), sort_keys=False),
                   encoding="utf-8")
    ensure_workdir(Path(directory) / ".brevet")
    typer.echo(f"wrote {out} and .brevet/; set identity_policy.owner to your human:<email>")


@app.command()
def dream(
    workdir: str = ".brevet",
    manifest_path: str = "agent.yaml",
    model_family: str = "gemma4",
    assist: str = typer.Option("none", help="model assist: none or ollama (local, drafts only, always logged)"),
    assist_model: str = typer.Option("gemma4:12b"),
) -> None:
    """The dream cycle: mine recurring overrides into candidates, with no authority until promoted."""
    from brevet.assist import from_name
    from brevet.consent import allowed
    ledger, store, _ = _load(ensure_workdir(workdir))
    s = dream_cycle(ledger, store, model_family=model_family,
                    assist=from_name(assist, assist_model),
                    consent=allowed(_manifest_if_any(manifest_path), ledger))
    extra = f" {_plural(s['superseded'], 'older candidate')} superseded." if s["superseded"] else ""
    typer.echo(f"Dream: {_plural(s['overrides'], 'override')} mined into "
               f"{_plural(s['candidates'], 'new candidate')} and "
               f"{_plural(s['eval_cases'], 'new eval case')}, all at the Evidence layer "
               f"(no authority).{extra}")
    if s.get("without_consent"):
        typer.echo(f"{_plural(s['without_consent'], 'override')} left out: no consent from "
                   f"the participant (runtime_safety.dream).")


@app.command()
def dawn(
    workdir: str = ".brevet",
    decide: str = typer.Option(None, help="capability_id:outcome, where outcome is promote, hold, reject or re_elicit"),
    approver: str = typer.Option(None, help="who decides: human:<email> or mission_group:<name>"),
    layer: str = typer.Option("advisory", help="authority layer to promote to: advisory or controlled (needs a mission group)"),
) -> None:
    """The dawn gate: list pending candidates, or record one decision."""
    wd = ensure_workdir(workdir)
    ledger, store, _ = _load(wd)
    if decide is None:
        pending = store.pending()
        if not pending:
            typer.echo("Dawn: no candidates are pending.")
        for c in pending:
            typer.echo(f"  {c.capability_id}  [{c.kind.value}]  x{c.evidence.recurrence_count}  {c.title}")
        return
    if ":" not in decide:
        raise typer.BadParameter("write --decide as capability_id:outcome, "
                                 "for example cap_123:promote")
    if not approver:
        raise typer.BadParameter("--approver is required: dawn decisions need a named human or mission group")
    cap_id, outcome = decide.split(":", 1)
    if _signing_required(wd):
        req = request_promotion(wd, store, cap_id, outcome, approver=approver, to_layer=layer)
        _request_then_offer(wd, req, prefer=approver.strip())
        return
    cap = dawn_decide(store, ledger, cap_id, outcome, approver=approver,
                      to_layer=AuthorityLayer(layer))
    typer.echo(f"Dawn: {cap_id} is now {cap.validation_state.value} "
               f"(authority layer: {cap.authority_layer.value}), decided by {approver.strip()}.")


@app.command()
def release(
    manifest_path: str = "agent.yaml",
    workdir: str = ".brevet",
    to_version: str = typer.Option(...),
    channel: str = typer.Option("shadow", help="shadow, trial or production"),
    approver: str = typer.Option(..., help="human:<email> or mission_group:<name>"),
    eval_before: str = typer.Option(None, help="the 'before' eval run (its run_ref; see brevet evals)"),
    eval_after: str = typer.Option(None, help="the 'after' eval run; the deltas then come from the runs"),
    delta_in: float = typer.Option(None, help="held-in delta you attest, when there are no eval runs"),
    delta_out: float = typer.Option(None, help="held-out delta you attest, when there are no eval runs"),
    rationale: str = typer.Option("", help="why this release is being made"),
) -> None:
    """Pass the conservative gate, then sign and release the next version with its capabilities.lock."""
    manifest = _read_manifest(manifest_path)
    wd = ensure_workdir(workdir)
    ledger, store, signer = _load(wd)
    mpath = Path(manifest_path)
    harness = inventory(manifest, manifest_path, workdir=wd)
    if bool(eval_before) != bool(eval_after):
        raise ValueError("give both --eval-before and --eval-after, or neither")
    summary = evidence(ledger, store, manifest, harness, to_version=to_version,
                       approver=approver, previous_lock=load_lock(mpath.parent / "capabilities.lock"),
                       evals=(eval_before, eval_after) if eval_before else None,
                       delta_in=delta_in, delta_out=delta_out)
    if _signing_required(wd):
        req = request_release(wd, manifest, store, to_version=to_version, channel=channel,
                              approver=approver, eval_summary=summary, rationale=rationale,
                              manifest_path=manifest_path, harness=harness)
        _warn_unmatched(manifest, manifest_path, wd)
        _request_then_offer(wd, req, prefer=approver.strip())
        return
    manifest, lock, record = do_release(
        manifest, store, ledger, signer,
        to_version=to_version, channel=ReleaseChannel(channel), approver=approver,
        eval_summary=summary, rationale=rationale, harness=harness,
    )
    publish(wd, manifest, lock, mpath)
    harness_note = (f" and {_plural(len(lock.harness), 'harness component')}"
                    if lock.harness else "")
    source = ("measured by eval runs" if summary["source"] == "measured"
              else f"deltas attested by {approver}")
    typer.echo(f"Release: {record.from_version} -> {record.to_version} on the {channel} "
               f"channel, signed ({source}); capabilities.lock lists "
               f"{_plural(len(lock.resolved), 'capability', 'capabilities')}{harness_note}.")
    _warn_unmatched(manifest, manifest_path, wd)


@app.command()
def rollback(
    to_version: str = typer.Argument(..., help="the earlier release to return to"),
    manifest_path: str = "agent.yaml",
    workdir: str = ".brevet",
    approver: str = typer.Option(..., help="human:<email> or mission_group:<name>"),
    as_version: str = typer.Option(None, help="version of the new release (default: next patch)"),
    channel: str = typer.Option(None, help="channel of the new release (default: as before)"),
    rationale: str = typer.Option("", help="why the agent is rolled back"),
    remove_added: bool = typer.Option(False, help="set aside harness files added since that release"),
) -> None:
    """Return the agent to an earlier release, as a new signed release."""
    manifest = _read_manifest(manifest_path)
    wd = _existing(workdir)
    ledger, store, signer = _load(wd)
    mpath = Path(manifest_path)
    if _signing_required(wd):
        req = request_rollback(wd, manifest, store, target=to_version, approver=approver,
                               as_version=as_version, channel=channel, rationale=rationale,
                               remove_added=remove_added, manifest_path=manifest_path)
        _request_then_offer(wd, req, prefer=approver.strip())
        return
    manifest, lock, record, plan = do_rollback(
        wd, ledger, store, signer, mpath, manifest, target=to_version, approver=approver,
        as_version=as_version, channel=channel, rationale=rationale,
        remove_added=remove_added)
    typer.echo(f"Rollback: {record.from_version} -> {record.to_version} restores release "
               f"{to_version}; {_plural(len(lock.resolved), 'capability', 'capabilities')} "
               f"locked, {_plural(len(plan['restored_files']), 'harness file')} restored.")
    for item in plan["dropped"]:
        typer.echo(f"  left out {item['capability_id']}: {item['why']}")


@app.command()
def evals(workdir: str = ".brevet") -> None:
    """List the recorded eval runs, to bind a release to its 'before' and 'after' runs."""
    ledger, _, _ = _load(_existing(workdir))
    runs = list(ledger.read("brevet.eval_run"))
    if not runs:
        typer.echo("Evals: no runs recorded; run agent.evaluate() in Python.")
        return
    for e in runs:
        b = e["body"]
        kind = "before (current release)" if b.get("baseline") else "after (candidate)"
        typer.echo(f"  {e['envelope_id']}  {e.get('created_at', '')[:19]}  {kind}  "
                   f"held-in {b.get('held_in_pass_rate', 0):.2f}  "
                   f"held-out {b.get('held_out_pass_rate', 0):.2f}  {b.get('n_cases', 0)} cases")


def _warn_unmatched(manifest: AgentManifest, manifest_path: str, wd: Path) -> None:
    missing = unmatched_patterns(manifest, manifest_path, workdir=wd)
    if missing:
        typer.echo(f"Warning: these bindings.harness_files patterns match no file, so nothing "
                   f"they name is under change control: {', '.join(missing)}")


@app.command()
def recall(
    capability_id: str,
    workdir: str = ".brevet",
    reason: str = typer.Option(...),
    reason_class: str = typer.Option("incorrect", help="incorrect, unsafe, consent_withdrawn, superseded, stale, compliance, duplicate (one copy of content another capability keeps) or other"),
    severity: str = typer.Option("high", help="low, medium, high or critical"),
    action: str = typer.Option("rollback", help="what affected releases should do: quarantine, rollback or re_review"),
    issued_by: str = typer.Option(..., help="human:<email> or mission_group:<name>"),
) -> None:
    """Recall a capability and flag every release whose lockfile contains it."""
    wd = _existing(workdir)
    ledger, store, _ = _load(wd)
    if _signing_required(wd):
        req = request_recall(wd, store, capability_id, reason=reason, issued_by=issued_by,
                             reason_class=reason_class, severity=severity, action=action)
        _request_then_offer(wd, req, prefer=issued_by.strip())
        return
    releases = [ReleaseRecord(**e["body"]) for e in ledger.read("brevet.release")]
    notice = do_recall(store, ledger, capability_id, reason=reason,
                       reason_class=reason_class, severity=severity,
                       issued_by=issued_by, releases=releases, action=action)
    typer.echo(f"Recall: {capability_id} recalled; "
               f"{_plural(len(notice.affected_releases), 'release')} that shipped it "
               f"flagged for {notice.action}.")


def _manifest_if_any(manifest_path: str) -> AgentManifest | None:
    return _read_manifest(manifest_path) if Path(manifest_path).exists() else None


@app.command()
def verify(workdir: str = ".brevet", manifest_path: str = "agent.yaml") -> None:
    """Replay the evidence chain, check it against its anchors and check every approval."""
    wd = _existing(workdir)
    ledger, _, _ = _load(wd)
    ok, n = ledger.verify()
    if ok:
        typer.echo(f"Verify: evidence chain intact ({n} envelopes).")
    else:
        typer.echo(f"Verify: evidence chain BROKEN at envelope {n + 1}; "
                   f"the {n} envelopes before it are intact.")
    anchored = check_anchors(ledger, wd, manifest=_manifest_if_any(manifest_path))
    if anchored["configured"]:
        for t in anchored["targets"]:
            state = (f"{t.get('anchors', 0)} head(s), latest at envelope {t.get('latest', 0)}"
                     if t.get("reachable") else "unreachable")
            typer.echo(f"Anchor {t['ref']}: {state}")
        for problem in anchored["problems"]:
            typer.echo(f"  {problem}")
        if anchored.get("unanchored"):
            typer.echo(f"  {anchored['unanchored']} governing step(s) since the last anchor; "
                       f"brevet anchor writes the current head")
    report = verify_approvals(ledger)
    if report["signing_required"] or report["register_changes"] or report["invalid"]:
        typer.echo(f"Approvals: {report['signed']} signed, "
                   f"{report['unsigned_before_signing']} recorded before signing was required, "
                   f"{len(report['invalid'])} invalid.")
        for item in report["invalid"]:
            typer.echo(f"  {item['kind']} {item['envelope_id']}: {item['problem']}")
    unverified = []
    if report["signing_required"] and ok and anchored["ok"]:
        try:
            policy = identity_policy(wd, manifest_path if Path(manifest_path).exists() else None)
        except PermissionError as e:
            policy = {}
            typer.echo(f"Approver identities: not checked ({e}).")
        if policy:
            ident = identity_report(register_from_chain(ledger), policy)
            unverified = ident["unverified"]
            checked = ident["approvers"] - len({i["identity"] for i in unverified + ident["unchecked"]})
            typer.echo(f"Approver identities: {checked} of {ident['approvers']} verified "
                       f"against the identity source.")
            for item in unverified + ident["unchecked"]:
                typer.echo(f"  {item['identity']}: {item['problem']}")
    raise typer.Exit(0 if ok and anchored["ok"] and not report["invalid"] and not unverified
                     else 1)


@app.command()
def anchor(workdir: str = ".brevet", manifest_path: str = "agent.yaml") -> None:
    """Write the evidence chain's head to its anchors outside the workspace."""
    wd = _existing(workdir)
    ledger, _, _ = _load(wd)
    out = anchor_head(ledger, wd, manifest=_manifest_if_any(manifest_path))
    if not out["targets"]:
        typer.echo(f"Anchor: {out.get('note', 'nothing to anchor')}. Add references under "
                   f"runtime_safety.evidence.anchors, in BREVET_ANCHORS or in "
                   f"~/.config/brevet/anchors, such as file:/Volumes/backup/brevet-anchors.jsonl.")
        raise typer.Exit(1)
    for t in out["targets"]:
        typer.echo(f"Anchor {t['ref']}: " + ("written" if t["written"] else
                                              t.get("error") or "queued, will retry"))
    typer.echo(f"Head: envelope {out.get('count')}, {out.get('chain_hash')}.")


@app.command()
def benchmark(workdir: str = ".brevet", agent: str = typer.Option(None, help="one agent's lineage")) -> None:
    """Score the workspace's lineage on the four governed-adaptation axes (BENCHMARK.md)."""
    from brevet.benchmark import profile
    ledger, store, _ = _load(_existing(workdir))
    typer.echo(json.dumps(profile(ledger, store, agent=agent), indent=2))


@app.command()
def recalls(workdir: str = ".brevet") -> None:
    """List recalls, the agents that shipped each capability, and their acknowledgements."""
    ledger, _, _ = _load(_existing(workdir))
    items = recall_status(ledger)
    if not items:
        typer.echo("Recalls: none.")
        return
    for r in items:
        state = "complete" if r["complete"] else f"waiting for {', '.join(r['waiting_for'])}"
        typer.echo(f"  {r['recall_id']}  {r['capability_id']}  {state}")
        for a in r["acknowledged"]:
            typer.echo(f"      acknowledged by {a['agent']} {a['release'] or ''} "
                       f"({a['serving_point']}): {a['how']}")
    if any(not r["complete"] for r in items):
        raise typer.Exit(1)


@app.command()
def status(workdir: str = ".brevet", manifest_path: str = "agent.yaml") -> None:
    """Show the version, capabilities by authority layer, and evidence-chain health."""
    ledger, store, _ = _load(_existing(workdir))
    by_layer: dict[str, int] = {}
    revoked = 0
    for c in store.all().values():
        if c.revocation_status.value == "active":
            by_layer[c.authority_layer.value] = by_layer.get(c.authority_layer.value, 0) + 1
        elif c.revocation_status.value == "withdrawn":
            revoked += 1
    ok, n = ledger.verify()
    line, m = "", None
    if Path(manifest_path).exists():
        m = _read_manifest(manifest_path)
        line = f"{m.agent} v{m.version} [{m.release.get('channel', 'shadow')}]  "
    typer.echo(f"{line}capabilities by layer: {by_layer or {} }  recalled: {revoked}  "
               f"pending at dawn: {len(store.pending())}  "
               f"evidence chain: {f'intact ({n} envelopes)' if ok else f'BROKEN at envelope {n + 1}'}")
    register = register_from_chain(ledger)
    if register.enabled:
        waiting = len(PendingRequests(Path(workdir)).pending())
        typer.echo(f"signed approvals: required ({len(register.active())} approver(s), "
                   f"{waiting} request(s) awaiting signatures)")
    refs = configured_anchors(Path(workdir), m)
    if refs:
        typer.echo(f"anchors: {len(refs)} configured ({', '.join(refs)})")
    open_recalls = [r for r in recall_status(ledger) if not r["complete"]]
    if open_recalls:
        typer.echo(f"recalls: {len(open_recalls)} waiting for acknowledgement "
                   f"(brevet recalls)")
    lock = load_lock(Path(manifest_path).parent / "capabilities.lock") if m else None
    if lock is not None and lock.harness_sources:
        drift = file_drift(lock, m, Path(workdir), Path(manifest_path))
        typer.echo(f"harness: {_plural(len(lock.harness), 'component')} locked "
                   f"({', '.join(lock.harness_sources)}); "
                   f"{_plural(len(drift), 'file')} changed since the release")


@app.command()
def playground(port: int = 8765, workdir: str = "",
               open_browser: bool = True) -> None:
    """Step through the governed evolution loop, stage by stage, in a browser."""
    from brevet.playground import serve
    serve(port=port, workdir=Path(workdir) if workdir else None,
          open_browser=open_browser)


@app.command()
def mcp(
    workdir: str = typer.Option(None, help="the workspace folder (default: $BREVET_WORKDIR, "
                                           "$BREVET_HOME/.brevet or ./.brevet)"),
    manifest_path: str = typer.Option(None, help="the manifest (default: $BREVET_MANIFEST, "
                                                 "$BREVET_HOME/agent.yaml or ./agent.yaml)"),
) -> None:
    """Serve the whole loop as tools to an MCP client such as Claude Desktop (stdio)."""
    # Errors go to stderr, never stdout: stdout belongs to the JSON-RPC
    # stream and any stray text corrupts it for the connected client.
    try:
        from brevet.mcp_server import serve
    except ImportError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    serve(workdir, manifest_path)


@app.command()
def harness(manifest_path: str = "agent.yaml", workdir: str = ".brevet") -> None:
    """Compare the harness files with the latest release and list what changed.

    Files are declared in the manifest under bindings.harness_files. The live
    agent's components and library versions are checked by brevet.wrap()
    before each run, since only the running program can see them."""
    manifest = _read_manifest(manifest_path)
    lock = load_lock(Path(manifest_path).parent / "capabilities.lock")
    if lock is None:
        typer.echo("Harness: no release yet, so nothing to compare.")
        return
    locked = {c.kind for c in lock.harness}
    typer.echo(f"Harness: release {lock.agent_version} locks "
               f"{_plural(len(lock.harness), 'component')} "
               f"({', '.join(lock.harness_sources) or 'none'}).")
    if not (manifest.bindings or {}).get("harness_files"):
        typer.echo("No harness files are declared; add bindings.harness_files to the manifest "
                   "to put prompts, skills, tool code or MCP configuration under change control.")
        return
    _warn_unmatched(manifest, manifest_path, Path(workdir))
    live, _ = inventory(manifest, manifest_path, workdir=workdir, sources=["files"])
    changes = compare([c for c in lock.harness if c.kind == "file"], live)
    if "file" not in locked and live and "files" not in lock.harness_sources:
        typer.echo("The latest release did not lock harness files; the next release will.")
        return
    if not changes:
        typer.echo("Harness files: no changes since that release.")
        return
    for c in changes:
        typer.echo(f"  {c['change']:8}  {c['component_id']}")
    typer.echo(f"{_plural(len(changes), 'harness file')} changed without a release.")
    raise typer.Exit(1)


@app.command()
def approve(
    request_ids: Annotated[list[str] | None, typer.Argument(
        help="requests to sign; with none, list them")] = None,
    all_pending: bool = typer.Option(False, "--all", help="sign every pending request"),
    identity: str = typer.Option(None, help="the local approver key to sign with"),
    workdir: str = typer.Option(".brevet"),
    manifest_path: str = typer.Option(None, help="the manifest a release writes "
                                                 "(default: the one recorded with the request)"),
) -> None:
    """Review and sign pending decisions; each is applied once its signatures suffice."""
    wd = _existing(workdir)
    pending = PendingRequests(wd).pending()
    if not request_ids and not all_pending:
        if not pending:
            typer.echo("No requests are waiting for signatures.")
            return
        for r in pending:
            signed = ", ".join(s["identity"] for s in r["signatures"]) or "no signatures yet"
            typer.echo(f"  {r['request_id']}  {r['summary']}  ({signed})")
        typer.echo("Sign with: brevet approve <request_id> ... or brevet approve --all")
        return
    known = {r["request_id"]: r for r in pending}
    chosen = list(known) if all_pending else list(request_ids)
    unknown = [rid for rid in chosen if rid not in known]
    if unknown:
        raise KeyError(f"not waiting for signatures: {', '.join(unknown)}")
    for rid in chosen:
        typer.echo(f"  {rid}  {known[rid]['summary']}")
    _sign_and_apply(wd, chosen, identity=identity, manifest_path=manifest_path)


@approver_app.command("add")
def approver_add(
    identity: str = typer.Option(..., help="the person, written human:<email>"),
    group: Annotated[list[str] | None, typer.Option(
        "--group", help="a mission group they belong to; repeat for several")] = None,
    ssh_key: str = typer.Option(None, help="use this Ed25519 SSH key as the approver key"),
    github: str = typer.Option(None, help="their GitHub account, which must publish the key"),
    workdir: str = typer.Option(".brevet"),
    manifest_path: str = typer.Option(None, help="the manifest whose identity policy applies "
                                                 "(default: agent.yaml beside the workspace)"),
) -> None:
    """Create a passphrase-protected approver key (or use an SSH key) and register it."""
    wd = ensure_workdir(workdir)
    first = not _signing_required(wd)
    if ssh_key:
        use_ssh_key(identity, ssh_key)
        key = _unlock(identity)  # the SSH key signs its own registration
    else:
        passphrase = getpass.getpass(f"New passphrase for {identity}: ")
        if getpass.getpass("Repeat the passphrase: ") != passphrase:
            raise ValueError("the passphrases differ")
        key = create_key(identity, passphrase)
    req = request_register_add(wd, key, group, github=github, manifest_path=manifest_path)
    out = apply_if_ready(wd, req["request_id"], manifest_path=manifest_path)
    if out["status"] == "applied":
        groups = f" in {', '.join(sorted(group))}" if group else ""
        typer.echo(f"Registered {key.identity}{groups}; the key is in {key_dir()}.")
        if first:
            typer.echo("Signed approvals are now required for dawn decisions, releases and "
                       "recalls in this workspace. Release again with their signatures "
                       "(brevet release) before running outside shadow or serving rules: "
                       "only signed releases count from now on.")
    else:
        typer.echo(f"Key created in {key_dir()}. Registration request {req['request_id']} "
                   f"needs another approver: brevet approve {req['request_id']}")


@approver_app.command("list")
def approver_list(workdir: str = typer.Option(".brevet"),
                  manifest_path: str = typer.Option(
                      None, help="the manifest whose identity policy applies "
                                 "(default: agent.yaml beside the workspace)")) -> None:
    """Show the registered approvers, their groups and group thresholds."""
    register = register_from_chain(Ledger(_existing(workdir) / "ledger.jsonl"))
    if not register.approvers:
        typer.echo("No approvers are registered; decisions are recorded unsigned.")
    try:
        policy = identity_policy(Path(workdir), manifest_path)
    except PermissionError as e:
        typer.echo(f"Identity checks unavailable: {e}")
        policy = {}
    for who, entry in sorted(register.approvers.items()):
        state = "active" if entry["active"] else "revoked"
        groups = ", ".join(entry["groups"]) or "no groups"
        checked = ""
        if policy and entry["active"]:
            problems = identity_problems({"identity": who, "public_key": entry["public_key"],
                                          "github": entry.get("github")}, policy)
            checked = "  identity verified" if not problems else f"  NOT VERIFIED: {problems[0]}"
        typer.echo(f"  {who}  {state}  key {entry['public_key'][:16]}...  {groups}{checked}")
    for group, count in sorted(register.thresholds.items()):
        typer.echo(f"  {group} needs {count} signature(s)")
    local = local_identities()
    if local:
        typer.echo(f"Approver keys on this machine: {', '.join(local)}")


@approver_app.command("revoke")
def approver_revoke(identity: str = typer.Option(...),
                    workdir: str = typer.Option(".brevet")) -> None:
    """Revoke an approver; another approver signs the request."""
    wd = _existing(workdir)
    _request_then_offer(wd, request_register_revoke(wd, identity))


@approver_app.command("threshold")
def approver_threshold(group: str = typer.Option(..., help="mission_group:<name>"),
                       count: int = typer.Option(..., help="signatures its decisions need"),
                       workdir: str = typer.Option(".brevet")) -> None:
    """Set how many members of a mission group must sign its decisions."""
    wd = _existing(workdir)
    _request_then_offer(wd, request_threshold(wd, group, count))


@app.command()
def demo(directory: str = "brevet_demo") -> None:
    """Run the governed evolution loop once on synthetic deviation-triage data, offline."""
    from brevet.demo import run_demo
    run_demo(Path(directory), echo=typer.echo)


_EXPECTED = (PermissionError, ValueError, KeyError, FileNotFoundError, FileExistsError,
             RuntimeError)


@app.command()
def hook(workdir: str = typer.Option(None, help="the workspace folder (default: as for brevet mcp)"),
         manifest_path: str = typer.Option(None, help="the manifest (default: as for brevet mcp)"),
         ) -> None:
    """Check one Claude Code tool call against the released tool tiers (a PreToolUse hook).

    Reads the hook event from standard input and answers on standard output:
    nothing when the call is allowed, a denial (or a request to ask you, for
    a controlled_act tool) when it is not. The tiers and the channel come from
    the latest release, never from unreleased edits to agent.yaml. Anything
    that stops the check (no workspace, an unreadable event or manifest) blocks
    the call: Claude Code treats exit code 2 as a refusal."""
    import sys

    from brevet.broker import hook_decision
    from brevet.releases import released_manifest
    from brevet.workdir import resolve_workspace
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            raise TypeError("the hook event is not an object")
        wd, mpath = resolve_workspace(workdir, manifest_path)
        if not mpath.exists():
            raise FileNotFoundError(f"no manifest at {mpath}")
        ledger = Ledger(ensure_workdir(wd) / "ledger.jsonl", anchoring=False)
        manifest, channel = released_manifest(wd, mpath, ledger)
        ledger.manifest = manifest
        decision = hook_decision(event, manifest, ledger, channel=channel)
    except Exception as e:  # noqa: BLE001 - every failure must refuse the call
        typer.echo(f"Brevet could not check this tool call, so it is refused: {e}", err=True)
        raise typer.Exit(2) from None
    if decision:
        typer.echo(json.dumps(decision))


@consent_app.command("withdraw")
def consent_withdraw(
    participant: str = typer.Option(..., help="whose consent is withdrawn, e.g. human:<email>"),
    issued_by: str = typer.Option(..., help="who records it: human:<email> or mission_group:<name>"),
    reason: str = typer.Option("", help="why"),
    workdir: str = typer.Option(".brevet"),
) -> None:
    """Stop learning from a participant and recall every capability built on their overrides."""
    wd = _existing(workdir)
    ledger, store, _ = _load(wd)
    out = record_withdrawal(ledger, store, participant, issued_by=issued_by, reason=reason)
    typer.echo(f"Consent withdrawn for {out['participant']}: their {out['overrides']} "
               f"override(s) no longer count.")
    releases_ = [ReleaseRecord(**e["body"]) for e in ledger.read("brevet.release")]
    for cap_id in out["derived"]:
        why = f"consent withdrawn by {out['participant']}"
        if _signing_required(wd):
            req = request_recall(wd, store, cap_id, reason=why, issued_by=issued_by,
                                 reason_class="consent_withdrawn", action="quarantine")
            typer.echo(f"  recall of {cap_id} requested: brevet approve {req['request_id']}")
            continue
        notice = do_recall(store, ledger, cap_id, reason=why, reason_class="consent_withdrawn",
                           severity="high", issued_by=issued_by, releases=releases_,
                           action="quarantine")
        typer.echo(f"  recalled {cap_id} ({notice.recall_id})")


def main() -> None:
    """Run the CLI. Refusals and bad input print one clear line, not a traceback."""
    try:
        app()
    except _EXPECTED as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        typer.echo(f"error: {msg}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
