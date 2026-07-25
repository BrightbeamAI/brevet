"""Brevet CLI.

    brevet init          scaffold agent.yaml + .brevet workdir
    brevet demo          run the entire governed evolution loop on synthetic
                         data: record -> dream -> dawn -> release -> recall -> verify
    brevet dream         mine the ledger into candidate capability objects
    brevet dawn          list / decide pending candidates
    brevet release       sign and release the next harness version
    brevet recall        revoke a capability and flag affected releases
    brevet verify        replay the evidence chain independently
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
    typer.echo(f"brevet {__version__}")


@app.command()
def init(agent: str = "my_agent", directory: str = ".") -> None:
    """Scaffold a signature-conformant agent.yaml and workdir."""
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
    assist: str = typer.Option("none", help="drafting assist: none | ollama (local, logged, drafts only)"),
    assist_model: str = typer.Option("gemma4:12b"),
) -> None:
    """Run the delta engine over the ledger; emit Evidence-layer candidates."""
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
    typer.echo(f"dream: {len(overrides)} overrides -> {len(candidates)} candidates, "
               f"{len(evals)} compiled eval cases (all Evidence layer, no authority)")


@app.command()
def dawn(
    workdir: str = ".brevet",
    decide: str = typer.Option(None, help="capability_id:outcome (promote|hold|reject|re_elicit)"),
    approver: str = typer.Option(None, help="human:<email> or mission_group:<name>"),
    layer: str = typer.Option("advisory", help="target layer when promoting"),
) -> None:
    """Morning review: list pending candidates, or apply one decision."""
    wd = _workdir(workdir)
    ledger, store, _ = _load(wd)
    if decide is None:
        pending = store.pending()
        if not pending:
            typer.echo("dawn: nothing pending")
        for c in pending:
            typer.echo(f"  {c.capability_id}  [{c.kind.value}]  x{c.evidence.recurrence_count}  {c.title}")
        return
    cap_id, outcome = decide.split(":", 1)
    if not approver:
        raise typer.BadParameter("--approver is required for dawn decisions")
    cap = dawn_decide(store, ledger, cap_id, outcome, approver=approver,
                      to_layer=AuthorityLayer(layer))
    typer.echo(f"dawn: {cap_id} -> {cap.validation_state.value} ({cap.authority_layer.value})")


@app.command()
def release(
    manifest_path: str = "agent.yaml",
    workdir: str = ".brevet",
    to_version: str = typer.Option(...),
    channel: str = typer.Option("shadow"),
    approver: str = typer.Option(...),
    delta_in: float = typer.Option(0.0, help="held-in eval delta"),
    delta_out: float = typer.Option(0.0, help="held-out eval delta"),
) -> None:
    """Merge promoted capabilities, sign, and release the next version."""
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
    typer.echo(f"release: {record.from_version} -> {record.to_version} [{channel}] "
               f"{len(lock.resolved)} capabilities locked; manifest signed")


@app.command()
def recall(
    capability_id: str,
    workdir: str = ".brevet",
    reason: str = typer.Option(...),
    reason_class: str = typer.Option("incorrect"),
    severity: str = typer.Option("high"),
    issued_by: str = typer.Option(...),
) -> None:
    """Revoke a capability; flag every release whose lockfile contains it."""
    wd = _workdir(workdir)
    ledger, store, _ = _load(wd)
    releases = [ReleaseRecord(**e["body"]) for e in ledger.read("brevet.release")]
    notice = do_recall(store, ledger, capability_id, reason=reason,
                       reason_class=reason_class, severity=severity,
                       issued_by=issued_by, releases=releases)
    typer.echo(f"recall: {capability_id} revoked; "
               f"{len(notice.affected_releases)} release(s) flagged for {notice.action}")


@app.command()
def verify(workdir: str = ".brevet") -> None:
    """Independently replay the hash chain."""
    wd = _workdir(workdir)
    ledger, _, _ = _load(wd)
    ok, n = ledger.verify()
    typer.echo(f"verify: {'OK' if ok else 'BROKEN'} ({n} envelopes)")
    raise typer.Exit(0 if ok else 1)


@app.command()
def status(workdir: str = ".brevet", manifest_path: str = "agent.yaml") -> None:
    """Agent status: version, channel, capabilities by layer, chain integrity."""
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
    typer.echo(f"{line}capabilities={by_layer or {} }  revoked={revoked}  "
               f"pending={len(store.pending())}  chain={'OK' if ok else 'BROKEN'}/{n}")


@app.command()
def playground(port: int = 8765, workdir: str = "",
               open_browser: bool = True) -> None:
    """Run the governed evolution loop step by step in a local web UI,
    against a real workspace: real envelopes, signatures, and invariants."""
    from brevet.playground import serve
    serve(port=port, workdir=Path(workdir) if workdir else None,
          open_browser=open_browser)


@app.command()
def mcp(workdir: str = ".brevet", manifest_path: str = "agent.yaml") -> None:
    """Serve the full Brevet lifecycle to any MCP client (stdio)."""
    try:
        from brevet.mcp_server import serve
    except ImportError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(1)
    serve(workdir, manifest_path)


@app.command()
def demo(directory: str = "brevet_demo") -> None:
    """The whole loop, deterministically, on synthetic deviation-triage data."""
    from brevet.demo import run_demo
    run_demo(Path(directory), echo=typer.echo)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
