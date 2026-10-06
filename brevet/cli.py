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
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml

from brevet import __version__
from brevet.canonical import Signer
from brevet.delta import load_overrides, mine
from brevet.evals import compile_suite
from brevet.ledger import Ledger
from brevet.lifecycle import CapabilityStore, dawn_decide
from brevet.lifecycle import recall as do_recall
from brevet.lifecycle import release as do_release
from brevet.models import AgentManifest, AuthorityLayer, ReleaseChannel, ReleaseRecord

app = typer.Typer(add_completion=False, help=__doc__)


def _workdir(path: str = ".brevet") -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load(workdir: Path) -> tuple[Ledger, CapabilityStore, Signer]:
    return (
        Ledger(workdir / "ledger.jsonl"),
        CapabilityStore(workdir / "capabilities.jsonl"),
        Signer(workdir / "keys" / "brevet_ed25519.pem"),
    )


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
    overrides recorded with record_final, and a verdict already recorded is
    never counted twice."""
    from brevet.chap_evidence import ingest

    summary = ingest(source, workdir=_workdir(workdir), workspace=workspace,
                     strict=strict)
    typer.echo(
        f"Imported from CHAP (chain check: {summary['chain']}): "
        f"{summary['overrides']} overrides, {summary['approvals']} approvals, "
        f"{summary['rejections']} rejections from {len(summary['workspaces'])} workspace(s); "
        f"{summary.get('duplicates_skipped', 0)} already recorded and skipped.")


@app.command()
def init(agent: str = "my_agent", directory: str = ".") -> None:
    """Create a starter manifest (agent.yaml) and the .brevet workdir."""
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
    out = Path(directory) / "agent.yaml"
    out.write_text(yaml.safe_dump(manifest.model_dump(exclude_none=False), sort_keys=False))
    _workdir(str(Path(directory) / ".brevet"))
    typer.echo(f"wrote {out} and .brevet/")


@app.command()
def dream(
    workdir: str = ".brevet",
    model_family: str = "gemma4",
    assist: str = typer.Option("none", help="model assist: none or ollama (local, drafts only, always logged)"),
    assist_model: str = typer.Option("gemma4:12b"),
) -> None:
    """The dream cycle: mine recurring overrides into candidates, with no authority until promoted."""
    from brevet.assist import from_name
    wd = _workdir(workdir)
    ledger, store, _ = _load(wd)
    overrides = load_overrides(ledger)
    candidates = mine(overrides, model_family=model_family,
                      assist=from_name(assist, assist_model), ledger=ledger)
    for cand in candidates:
        store.add(cand)
        ledger.append("brevet.candidate", {"capability_id": cand.capability_id,
                                           "title": cand.title,
                                           "recurrence": cand.evidence.recurrence_count})
    evals = compile_suite([o for o in overrides if not o.intent_preserved])
    for case in evals:
        store.add(case)
    typer.echo(f"Dream: {len(overrides)} overrides mined into {len(candidates)} candidates "
               f"and {len(evals)} eval cases, all at the Evidence layer (no authority).")


@app.command()
def dawn(
    workdir: str = ".brevet",
    decide: str = typer.Option(None, help="capability_id:outcome, where outcome is promote, hold, reject or re_elicit"),
    approver: str = typer.Option(None, help="who decides: human:<email> or mission_group:<name>"),
    layer: str = typer.Option("advisory", help="authority layer to promote to: advisory or controlled"),
) -> None:
    """The dawn gate: list pending candidates, or record one decision."""
    wd = _workdir(workdir)
    ledger, store, _ = _load(wd)
    if decide is None:
        pending = store.pending()
        if not pending:
            typer.echo("Dawn: no candidates are pending.")
        for c in pending:
            typer.echo(f"  {c.capability_id}  [{c.kind.value}]  x{c.evidence.recurrence_count}  {c.title}")
        return
    cap_id, outcome = decide.split(":", 1)
    if not approver:
        raise typer.BadParameter("--approver is required: dawn decisions need a named human or mission group")
    cap = dawn_decide(store, ledger, cap_id, outcome, approver=approver,
                      to_layer=AuthorityLayer(layer))
    typer.echo(f"Dawn: {cap_id} is now {cap.validation_state.value} "
               f"(authority layer: {cap.authority_layer.value}), decided by {approver}.")


@app.command()
def release(
    manifest_path: str = "agent.yaml",
    workdir: str = ".brevet",
    to_version: str = typer.Option(...),
    channel: str = typer.Option("shadow"),
    approver: str = typer.Option(...),
    delta_in: float = typer.Option(0.0, help="held-in eval delta (change in pass rate)"),
    delta_out: float = typer.Option(0.0, help="held-out eval delta (change in pass rate)"),
) -> None:
    """Pass the conservative gate, then sign and release the next version with its capabilities.lock."""
    wd = _workdir(workdir)
    ledger, store, signer = _load(wd)
    manifest = AgentManifest(**yaml.safe_load(Path(manifest_path).read_text()))
    manifest, lock, record = do_release(
        manifest, store, ledger, signer,
        to_version=to_version, channel=ReleaseChannel(channel), approver=approver,
        eval_summary={"delta_held_in": delta_in, "delta_held_out": delta_out,
                      "gate": "conservative"},
    )
    Path(manifest_path).write_text(
        yaml.safe_dump(manifest.model_dump(exclude_none=False), sort_keys=False))
    lock_path = Path(manifest_path).parent / "capabilities.lock"
    lock_path.write_text(lock.model_dump_json(indent=2))
    typer.echo(f"Release: {record.from_version} -> {record.to_version} on the {channel} "
               f"channel, signed; capabilities.lock lists {len(lock.resolved)} capabilities.")


@app.command()
def recall(
    capability_id: str,
    workdir: str = ".brevet",
    reason: str = typer.Option(...),
    reason_class: str = typer.Option("incorrect"),
    severity: str = typer.Option("high"),
    issued_by: str = typer.Option(...),
) -> None:
    """Recall a capability and flag every release whose lockfile contains it."""
    wd = _workdir(workdir)
    ledger, store, _ = _load(wd)
    releases = [ReleaseRecord(**e["body"]) for e in ledger.read("brevet.release")]
    notice = do_recall(store, ledger, capability_id, reason=reason,
                       reason_class=reason_class, severity=severity,
                       issued_by=issued_by, releases=releases)
    typer.echo(f"Recall: {capability_id} recalled; {len(notice.affected_releases)} "
               f"release(s) that shipped it flagged for {notice.action}.")


@app.command()
def verify(workdir: str = ".brevet") -> None:
    """Replay the hash-linked evidence chain to detect edits."""
    wd = _workdir(workdir)
    ledger, _, _ = _load(wd)
    ok, n = ledger.verify()
    if ok:
        typer.echo(f"Verify: evidence chain intact ({n} envelopes).")
    else:
        typer.echo(f"Verify: evidence chain BROKEN at envelope {n + 1}; "
                   f"the {n} envelopes before it are intact.")
    raise typer.Exit(0 if ok else 1)


@app.command()
def status(workdir: str = ".brevet", manifest_path: str = "agent.yaml") -> None:
    """Show the version, capabilities by authority layer, and evidence-chain health."""
    wd = _workdir(workdir)
    ledger, store, _ = _load(wd)
    by_layer: dict[str, int] = {}
    revoked = 0
    for c in store.all().values():
        if c.revocation_status.value == "active":
            by_layer[c.authority_layer.value] = by_layer.get(c.authority_layer.value, 0) + 1
        else:
            revoked += 1
    ok, n = ledger.verify()
    line = ""
    if Path(manifest_path).exists():
        m = AgentManifest(**yaml.safe_load(Path(manifest_path).read_text()))
        line = f"{m.agent} v{m.version} [{m.release.get('channel', 'shadow')}]  "
    typer.echo(f"{line}capabilities by layer: {by_layer or {} }  recalled: {revoked}  "
               f"pending at dawn: {len(store.pending())}  "
               f"evidence chain: {f'intact ({n} envelopes)' if ok else f'BROKEN at envelope {n + 1}'}")


@app.command()
def playground(port: int = 8765, workdir: str = "",
               open_browser: bool = True) -> None:
    """Step through the governed evolution loop, stage by stage, in a browser."""
    from brevet.playground import serve
    serve(port=port, workdir=Path(workdir) if workdir else None,
          open_browser=open_browser)


@app.command()
def mcp(workdir: str = ".brevet", manifest_path: str = "agent.yaml") -> None:
    """Serve the whole loop as tools to an MCP client such as Claude Desktop (stdio)."""
    try:
        from brevet.mcp_server import serve
    except ImportError as e:
        # stderr, never stdout: stdout belongs to the JSON-RPC stream and
        # any stray text corrupts it for the connected MCP client.
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    serve(workdir, manifest_path)


@app.command()
def demo(directory: str = "brevet_demo") -> None:
    """Run the governed evolution loop once on synthetic deviation-triage data, offline."""
    from brevet.demo import run_demo
    run_demo(Path(directory), echo=typer.echo)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
