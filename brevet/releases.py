"""Publishing releases, keeping their archive, and rolling back.

Every release writes ``capabilities.lock`` and the signed manifest in one
step each, and keeps a copy of what it shipped in the workspace:

    releases/<version>.json    the signed manifest, the lock and the digest
                               of every harness file the release named
    objects/<sha256>           the content of those files, stored once by
                               digest however many releases share them

A rollback returns the agent to an earlier release as a new, signed release:
the earlier manifest and harness files come back, the capability set is the
one that release locked minus anything recalled or decided against since,
and the release record names the version it restores. The chain stays
append-only; nothing is deleted. Code that lives outside the named files
(the agent program itself) is restored from version control; until it
matches the restored release, the drift check stops the agent outside the
shadow channel.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from brevet.canonical import object_sha256
from brevet.harness import _collect, _file_digest, _patterns, lock_digest
from brevet.models import AgentManifest, CapabilitiesLock, HarnessComponent
from brevet.workdir import write_atomic


def lock_path_for(workdir: Path, manifest_path: Path | None) -> Path:
    return (manifest_path.parent if manifest_path else workdir) / "capabilities.lock"


def _file_paths(manifest: AgentManifest, manifest_path: Path | None,
                workdir: Path) -> dict[str, Path]:
    base = manifest_path.parent if manifest_path else Path(".")
    exclude = [manifest_path, base / "capabilities.lock"] if manifest_path else None
    return _collect(_patterns(manifest), base, exclude, [workdir])


def _path_of(label: str, manifest_path: Path | None) -> Path:
    if ".." in Path(label).parts:
        raise ValueError(f"refusing the harness file label {label!r}: it climbs out of its folder")
    if label.startswith("~/"):
        return Path.home() / label[2:]
    if label.startswith("/"):
        return Path(label)
    return (manifest_path.parent if manifest_path else Path(".")) / label


def _safe_path(label: str, manifest_path: Path | None) -> Path:
    """Where a harness file label lives, refused when a symlink would carry a
    write somewhere else."""
    path = _path_of(label, manifest_path)
    if label.startswith(("~/", "/")):
        lexical = os.path.normpath(os.path.abspath(path))
    else:
        base = os.path.realpath(manifest_path.parent if manifest_path else Path("."))
        lexical = os.path.normpath(os.path.join(base, label))
    if os.path.realpath(path) != lexical:
        raise ValueError(f"refusing {label}: a symlink on its path leads elsewhere")
    return path


def _object(workdir: Path, digest: str) -> bytes | None:
    """The archived content for a digest, only if it still hashes to it."""
    path = workdir / "objects" / digest.split(":", 1)[-1]
    if not path.is_file():
        return None
    data = path.read_bytes()
    return data if "sha256:" + hashlib.sha256(data).hexdigest() == digest else None


def archive(workdir: Path, manifest: AgentManifest, lock: CapabilitiesLock,
            manifest_path: Path | None) -> Path:
    """Keep the release's manifest, lock and named files in the workspace."""
    objects = workdir / "objects"
    files: dict[str, str] = {}
    locked = {c.component_id[5:]: c.digest for c in lock.harness if c.kind == "file"}
    for label, path in _file_paths(manifest, manifest_path, workdir).items():
        if label not in locked:
            continue
        digest = _file_digest(path)
        if digest != locked[label]:
            continue  # changed after the inventory was taken; the lock is authoritative
        target = objects / digest.split(":", 1)[1]
        if not target.exists():
            objects.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(path.read_bytes())
            tmp.replace(target)
        files[label] = digest
    record = {"agent": lock.agent, "version": lock.agent_version,
              "manifest": manifest.model_dump(mode="json"),
              "lock": lock.model_dump(mode="json"), "files": files}
    out = workdir / "releases" / f"{lock.agent_version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(out, json.dumps(record, indent=2, sort_keys=True))
    return out


def publish(workdir: Path, manifest: AgentManifest, lock: CapabilitiesLock,
            manifest_path: Path | None) -> None:
    """Write the lock and the signed manifest, each atomically, and archive
    the release. The lock goes first, so a reader never sees a manifest that
    names a lock not yet written."""
    lp = lock_path_for(workdir, manifest_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(lp, lock.model_dump_json(indent=2))
    if manifest_path:
        write_atomic(manifest_path, yaml.safe_dump(manifest.model_dump(exclude_none=False),
                                                   sort_keys=False))
    archive(workdir, manifest, lock, manifest_path)


def archived(workdir: Path, version: str | None) -> bool:
    return bool(version) and (workdir / "releases" / f"{version}.json").exists()


def load_archive(workdir: Path, version: str) -> dict[str, Any] | None:
    path = workdir / "releases" / f"{version}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def next_patch(version: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def plan_rollback(workdir: Path, ledger, store, manifest_path: Path | None,
                  target: str) -> dict[str, Any]:
    """What rolling back to release ``target`` would ship and restore,
    checked against the evidence chain."""
    from brevet.approvals import verify_envelopes
    from brevet.serving import chain_facts

    found = load_archive(workdir, target)
    if found is None:
        raise FileNotFoundError(f"release {target} has no archive in {workdir / 'releases'}; "
                                f"releases are archived from Brevet 0.4.0 on")
    restored = AgentManifest(**found["manifest"])
    old_lock = CapabilitiesLock(**found["lock"])
    envelopes = list(ledger.read())
    invalid = {i["envelope_id"] for i in verify_envelopes(envelopes)["invalid"]}
    recorded = [e["body"] for e in envelopes if e.get("kind") == "brevet.release"
                and e.get("envelope_id") not in invalid
                and e["body"].get("agent") == restored.agent
                and e["body"].get("to_version") == target]
    if not recorded:
        raise ValueError(f"the evidence chain has no release {target} of {restored.agent}")
    body = recorded[-1]
    if lock_digest(old_lock) != body.get("lockfile_hash"):
        raise ValueError(f"the archive of release {target} does not match the lock the "
                         f"evidence chain recorded for it")
    if body.get("manifest_hash") and \
            object_sha256(restored.unsigned_payload()) != body["manifest_hash"]:
        raise ValueError(f"the archive of release {target} does not match the manifest the "
                         f"evidence chain recorded for it")
    facts = chain_facts(ledger, invalid)
    caps = store.all()
    keep, dropped = set(), []
    for entry in old_lock.resolved:
        cap = caps.get(entry.capability_id)
        if facts["recalls"].get(entry.capability_id) or facts["recalls"].get(entry.content_hash):
            dropped.append({"capability_id": entry.capability_id, "why": "recalled"})
        elif entry.capability_id in facts["decided_against"]:
            dropped.append({"capability_id": entry.capability_id, "why": "decided against"})
        elif cap is None or cap.content_hash != entry.content_hash or not cap.releasable:
            dropped.append({"capability_id": entry.capability_id,
                            "why": "no longer releasable as it was"})
        else:
            keep.add(entry.capability_id)
    # What to restore comes from the archived lock, which the chain vouches for,
    # never from the archive's own file list; every object is re-hashed.
    restore, missing, locked = [], [], set()
    for comp in old_lock.harness:
        if comp.kind != "file":
            continue
        label = comp.component_id[5:]
        locked.add(label)
        path = _safe_path(label, manifest_path)
        current = _file_digest(path) if path.is_file() else None
        if current == comp.digest:
            continue
        if _object(workdir, comp.digest) is None:
            missing.append(label)
            continue
        restore.append({"label": label, "path": str(path), "digest": comp.digest,
                        "now": current})
    if missing:
        raise FileNotFoundError("the archive no longer holds the exact content of "
                                + ", ".join(missing))
    added = sorted(label for label in _file_paths(restored, manifest_path, workdir)
                   if label not in locked)
    for label in added:
        _safe_path(label, manifest_path)
    harness = ([HarnessComponent(**c) for c in found["lock"].get("harness") or []],
               list(found["lock"].get("harness_sources") or []))
    return {"target": target, "manifest": restored, "keep": keep, "dropped": dropped,
            "restore": restore, "added": added, "harness": harness,
            "channel": (restored.release or {}).get("channel", "shadow")}


def restore_files(workdir: Path, plan: dict[str, Any], manifest_path: Path | None,
                  *, remove_added: bool = False) -> list[str]:
    """Put back the content of every harness file the rollback restores, and
    set aside (never delete) files added since, when asked to."""
    from brevet.models import _now
    done = []
    for item in plan["restore"]:
        data = _object(workdir, item["digest"])
        if data is None:
            raise FileNotFoundError(f"the archived content of {item['label']} changed")
        path = Path(item["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.brevet-restore")
        tmp.write_bytes(data)
        tmp.replace(path)
        done.append(item["label"])
    if remove_added and plan["added"]:
        aside = workdir / "set-aside" / _now().replace(":", "-")
        for label in plan["added"]:
            src = _safe_path(label, manifest_path)
            if label.startswith("~/"):
                dest = aside / "home" / label[2:]
            elif label.startswith("/"):
                dest = aside / "root" / label.lstrip("/")
            else:
                dest = aside / "project" / label
            n = 1
            while dest.exists():  # never overwrite what was set aside before
                dest = dest.with_name(f"{dest.name}.{n}")
                n += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            done.append(f"{label} (set aside)")
    return done


def rollback_manifest(plan: dict[str, Any], current: AgentManifest) -> AgentManifest:
    """The earlier manifest's content, carried forward from the current version."""
    restored = plan["manifest"].model_copy(deep=True)
    restored.version = current.version
    restored.signature = None
    return restored


def prepare_rollback(workdir: Path, ledger, store, manifest_path: Path | None,
                     current: AgentManifest, *, target: str, approver: str,
                     as_version: str | None = None, channel: str | None = None,
                     rationale: str = "", remove_added: bool = False) -> dict[str, Any]:
    """Validate a rollback and return its plan with the payload an approver
    signs (a release that names the version it restores)."""
    from brevet.lifecycle import prepare_release
    plan = plan_rollback(workdir, ledger, store, manifest_path, target)
    if plan["added"] and not remove_added:
        raise ValueError(
            f"files added since release {target} would leave the restored agent "
            f"drifting: {', '.join(plan['added'][:5])}. Remove them, or roll back with "
            f"remove_added (--remove-added) to set them aside in the workspace")
    restored = rollback_manifest(plan, current)
    version = as_version or next_patch(current.version)
    chan = channel or plan["channel"]
    order = ("shadow", "trial", "production")
    if chan not in order or order.index(chan) > order.index(plan["channel"]):
        raise ValueError(f"a rollback returns to release {target} on its {plan['channel']} "
                         f"channel or a lower one, not {chan}; a wider channel needs a "
                         f"release with its own evidence")
    why = rationale or f"Roll back to {target}."
    set_aside = list(plan["added"]) if remove_added else []
    who, chan_, lock, payload = prepare_release(
        restored, store, to_version=version, channel=chan, approver=approver,
        eval_summary={"source": "rollback", "restores": target}, rationale=why,
        harness=plan["harness"], ledger=ledger, only=plan["keep"], restores=target,
        set_aside=set_aside)
    plan["set_aside"] = set_aside
    plan.update({"restored": restored, "version": version, "channel": chan_.value,
                 "rationale": why, "lock": lock, "payload": payload, "approver": who})
    return plan


def rollback(workdir: Path, ledger, store, signer, manifest_path: Path | None,
             current: AgentManifest, *, target: str, approver: str,
             as_version: str | None = None, channel: str | None = None,
             rationale: str = "", approval: dict[str, Any] | None = None,
             remove_added: bool = False):
    """Return the agent to release ``target`` as a new signed release, then
    restore the harness files that release named. Returns the signed
    manifest, the lock, the release record and the plan."""
    from brevet.approvals import enforce
    from brevet.lifecycle import release
    plan = prepare_rollback(workdir, ledger, store, manifest_path, current, target=target,
                            approver=approver, as_version=as_version, channel=channel,
                            rationale=rationale, remove_added=remove_added)
    enforce(ledger, plan["payload"], approval)  # refuse before anything on disk moves
    # files first: if a move fails, nothing is recorded, and running the rollback
    # again finishes it (files already restored or set aside are skipped)
    plan["restored_files"] = restore_files(workdir, plan, manifest_path,
                                           remove_added=remove_added)
    manifest, lock, record = release(
        plan["restored"], store, ledger, signer, to_version=plan["version"],
        channel=plan["channel"], approver=approver,
        eval_summary={"source": "rollback", "restores": target},
        rationale=plan["rationale"], approval=approval, harness=plan["harness"],
        only=plan["keep"], restores=target, set_aside=plan["set_aside"])
    publish(workdir, manifest, lock, manifest_path)
    return manifest, lock, record, plan


def evidence(ledger, store, manifest: AgentManifest, harness, *, to_version: str,
             approver: str, previous_lock: CapabilitiesLock | None,
             evals: tuple[Any, Any] | None = None, delta_in: float | None = None,
             delta_out: float | None = None) -> dict[str, Any]:
    """The eval summary a release records: bound to two eval runs when they
    are given (the deltas then come from the runs), otherwise the deltas the
    approver attests."""
    from brevet.evals import bind_evals
    from brevet.lifecycle import build_lock
    if evals is None or not all(evals):
        return {"source": "attested", "attested_by": approver,
                "delta_held_in": delta_in or 0.0, "delta_held_out": delta_out or 0.0,
                "gate": "conservative"}
    preview = build_lock(manifest.model_copy(update={"version": to_version}), store, harness,
                         ledger=ledger)
    first = not any(e["body"].get("agent") == manifest.agent
                    for e in ledger.read("brevet.release"))
    summary = bind_evals(ledger, evals[0], evals[1], agent=manifest.agent, lock=preview,
                         harness=harness, manifest=manifest, previous_lock=previous_lock,
                         store=store, first_release=first)
    for given, key in ((delta_in, "delta_held_in"), (delta_out, "delta_held_out")):
        if given is not None and abs(given - summary[key]) > 1e-9:
            raise ValueError(f"{key} {given} differs from the measured {summary[key]}; "
                             f"leave the deltas to the eval runs")
    return summary


def ensure_archive(workdir: Path, manifest: AgentManifest, lock: CapabilitiesLock | None,
                   manifest_path: Path | None, release: dict[str, Any]) -> None:
    """Archive a release made before releases were archived, the first time
    its manifest and lock are seen on disk exactly as released, so a later
    edit to agent.yaml cannot take the released manifest with it."""
    version = release.get("to_version")
    if lock is None or not version or archived(workdir, version):
        return
    if (not release.get("manifest_hash")
            or object_sha256(manifest.unsigned_payload()) != release["manifest_hash"]
            or lock.agent_version != version
            or lock_digest(lock) != release.get("lockfile_hash")):
        return
    try:
        archive(workdir, manifest, lock, manifest_path)
    except OSError:
        pass  # a read-only workspace still serves; the archive waits for the next look


def released_manifest(workdir: Path, manifest_path: Path, ledger) -> tuple[AgentManifest, str]:
    """The manifest of the latest release on the chain, with its channel, for
    policies that must not follow unreleased edits to agent.yaml (the tool
    hook, the approver identity policy). Raises when it cannot be
    established: the chain must replay intact and hold its anchored heads."""
    from brevet import anchor as anchors
    from brevet.approvals import verify_envelopes
    ok, n = ledger.verify()
    if not ok:
        raise PermissionError(f"the evidence chain is broken at envelope {n + 1}; "
                              f"run brevet verify")
    envelopes = list(ledger.read())
    invalid = {i["envelope_id"] for i in verify_envelopes(envelopes)["invalid"]}
    releases_ = [e["body"] for e in envelopes if e.get("kind") == "brevet.release"
                 and e.get("envelope_id") not in invalid]
    on_disk = AgentManifest(**(yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}))
    ours = [r for r in releases_ if r.get("agent") == on_disk.agent] or releases_
    released, channel = on_disk, "shadow"  # nothing released: nothing may act
    if ours:
        latest = ours[-1]
        channel = latest.get("channel", "shadow")
        found = load_archive(workdir, latest.get("to_version", ""))
        archived = AgentManifest(**found["manifest"]) if found is not None else None
        if archived is not None and (not latest.get("manifest_hash") or object_sha256(
                archived.unsigned_payload()) == latest["manifest_hash"]):
            released = archived
        elif latest.get("manifest_hash") and \
                object_sha256(on_disk.unsigned_payload()) == latest["manifest_hash"]:
            ensure_archive(workdir, on_disk, load_lock_file(lock_path_for(workdir, manifest_path)),
                           manifest_path, latest)
        elif not latest.get("manifest_hash"):
            raise ValueError(f"release {latest.get('to_version')} predates recorded manifest "
                             f"digests, so its manifest cannot be established; release again")
        else:
            raise ValueError(f"agent.yaml is not the manifest of release "
                             f"{latest.get('to_version')}, and that release has no archive to "
                             f"read it from; release again")
    refs = anchors.configured(workdir, on_disk)
    refs += [r for r in anchors.configured(workdir, released) if r not in refs]
    report = anchors.check(ledger, workdir, manifest=released, refs=refs)
    if not report["ok"]:
        raise PermissionError("the evidence chain does not match its anchors: "
                              + "; ".join(report["problems"]))
    return released, channel


def load_lock_file(path: Path) -> CapabilitiesLock | None:
    try:
        return CapabilitiesLock(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
