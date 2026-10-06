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
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
import yaml

from brevet import __version__
from brevet.canonical import Signer
from brevet.delta import dream_cycle
from brevet.ledger import Ledger
from brevet.lifecycle import CapabilityStore, dawn_decide
from brevet.lifecycle import recall as do_recall
from brevet.lifecycle import release as do_release
from brevet.models import AgentManifest, AuthorityLayer, ReleaseChannel, ReleaseRecord
from brevet.workdir import ensure_workdir

app = typer.Typer(add_completion=False, help=__doc__, pretty_exceptions_enable=False)


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
    ledger, store, _ = _load(ensure_workdir(workdir))
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
    ledger, store, signer = _load(ensure_workdir(workdir))
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
    ledger, store, _ = _load(_existing(workdir))
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
    raise typer.Exit(0 if ok else 1)


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
