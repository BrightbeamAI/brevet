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
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path
from typing import Annotated

import typer
import yaml

from brevet import __version__
from brevet.approvals import (
    ApproverKey,
    PendingRequests,
    apply_if_ready,
    create_key,
    key_dir,
    load_key,
    local_identities,
    register_from_chain,
    request_promotion,
    request_recall,
    request_register_add,
    request_register_revoke,
    request_release,
    request_threshold,
    sign_request,
    verify_approvals,
)
from brevet.canonical import Signer
from brevet.delta import dream_cycle
from brevet.ledger import Ledger
from brevet.lifecycle import CapabilityStore, dawn_decide
from brevet.lifecycle import recall as do_recall
from brevet.lifecycle import release as do_release
from brevet.models import AgentManifest, AuthorityLayer, ReleaseChannel, ReleaseRecord
from brevet.workdir import ensure_workdir

app = typer.Typer(add_completion=False, help=__doc__, pretty_exceptions_enable=False)
approver_app = typer.Typer(no_args_is_help=True, help=(
    "Register the people whose signatures this workspace requires. Once the first "
    "approver is registered, every dawn decision, release and recall must be signed."))
app.add_typer(approver_app, name="approver")


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
    model_family: str = "gemma4",
    assist: str = typer.Option("none", help="model assist: none or ollama (local, drafts only, always logged)"),
    assist_model: str = typer.Option("gemma4:12b"),
) -> None:
    """The dream cycle: mine recurring overrides into candidates, with no authority until promoted."""
    from brevet.assist import from_name
    ledger, store, _ = _load(ensure_workdir(workdir))
    s = dream_cycle(ledger, store, model_family=model_family,
                    assist=from_name(assist, assist_model))
    extra = f" {_plural(s['superseded'], 'older candidate')} superseded." if s["superseded"] else ""
    typer.echo(f"Dream: {_plural(s['overrides'], 'override')} mined into "
               f"{_plural(s['candidates'], 'new candidate')} and "
               f"{_plural(s['eval_cases'], 'new eval case')}, all at the Evidence layer "
               f"(no authority).{extra}")


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
    delta_in: float = typer.Option(0.0, help="held-in eval delta (change in pass rate)"),
    delta_out: float = typer.Option(0.0, help="held-out eval delta (change in pass rate)"),
    rationale: str = typer.Option("", help="why this release is being made"),
) -> None:
    """Pass the conservative gate, then sign and release the next version with its capabilities.lock."""
    manifest = _read_manifest(manifest_path)
    wd = ensure_workdir(workdir)
    ledger, store, signer = _load(wd)
    if _signing_required(wd):
        req = request_release(wd, manifest, store, to_version=to_version, channel=channel,
                              approver=approver,
                              eval_summary={"delta_held_in": delta_in, "delta_held_out": delta_out,
                                            "gate": "conservative"},
                              rationale=rationale, manifest_path=manifest_path)
        _request_then_offer(wd, req, prefer=approver.strip())
        return
    manifest, lock, record = do_release(
        manifest, store, ledger, signer,
        to_version=to_version, channel=ReleaseChannel(channel), approver=approver,
        eval_summary={"delta_held_in": delta_in, "delta_held_out": delta_out,
                      "gate": "conservative"},
        rationale=rationale,
    )
    Path(manifest_path).write_text(
        yaml.safe_dump(manifest.model_dump(exclude_none=False), sort_keys=False),
        encoding="utf-8")
    lock_path = Path(manifest_path).parent / "capabilities.lock"
    lock_path.write_text(lock.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(f"Release: {record.from_version} -> {record.to_version} on the {channel} "
               f"channel, signed; capabilities.lock lists "
               f"{_plural(len(lock.resolved), 'capability', 'capabilities')}.")


@app.command()
def recall(
    capability_id: str,
    workdir: str = ".brevet",
    reason: str = typer.Option(...),
    reason_class: str = typer.Option("incorrect", help="incorrect, unsafe, consent_withdrawn, superseded, stale, compliance or other"),
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


@app.command()
def verify(workdir: str = ".brevet") -> None:
    """Replay the hash-linked evidence chain to detect edits."""
    ledger, _, _ = _load(_existing(workdir))
    ok, n = ledger.verify()
    if ok:
        typer.echo(f"Verify: evidence chain intact ({n} envelopes).")
    else:
        typer.echo(f"Verify: evidence chain BROKEN at envelope {n + 1}; "
                   f"the {n} envelopes before it are intact.")
    report = verify_approvals(ledger)
    if report["signing_required"] or report["register_changes"] or report["invalid"]:
        typer.echo(f"Approvals: {report['signed']} signed, "
                   f"{report['unsigned_before_signing']} recorded before signing was required, "
                   f"{len(report['invalid'])} invalid.")
        for item in report["invalid"]:
            typer.echo(f"  {item['kind']} {item['envelope_id']}: {item['problem']}")
    raise typer.Exit(0 if ok and not report["invalid"] else 1)


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
    line = ""
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
    workdir: str = typer.Option(".brevet"),
) -> None:
    """Create a passphrase-protected approver key and register it."""
    wd = ensure_workdir(workdir)
    first = not _signing_required(wd)
    passphrase = getpass.getpass(f"New passphrase for {identity}: ")
    if getpass.getpass("Repeat the passphrase: ") != passphrase:
        raise ValueError("the passphrases differ")
    key = create_key(identity, passphrase)
    req = request_register_add(wd, key, group)
    out = apply_if_ready(wd, req["request_id"])
    if out["status"] == "applied":
        groups = f" in {', '.join(sorted(group))}" if group else ""
        typer.echo(f"Registered {key.identity}{groups}; the key is in {key_dir()}.")
        if first:
            typer.echo("Signed approvals are now required for dawn decisions, releases and "
                       "recalls in this workspace.")
    else:
        typer.echo(f"Key created in {key_dir()}. Registration request {req['request_id']} "
                   f"needs another approver: brevet approve {req['request_id']}")


@approver_app.command("list")
def approver_list(workdir: str = typer.Option(".brevet")) -> None:
    """Show the registered approvers, their groups and group thresholds."""
    register = register_from_chain(Ledger(_existing(workdir) / "ledger.jsonl"))
    if not register.approvers:
        typer.echo("No approvers are registered; decisions are recorded unsigned.")
    for who, entry in sorted(register.approvers.items()):
        state = "active" if entry["active"] else "revoked"
        groups = ", ".join(entry["groups"]) or "no groups"
        typer.echo(f"  {who}  {state}  key {entry['public_key'][:16]}...  {groups}")
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
