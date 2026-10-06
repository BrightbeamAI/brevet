"""Signed approvals: proof that a named person took each decision.

An approver holds an Ed25519 key that lives outside the workspace, encrypted
with a passphrase only they know. An agent that can call every Brevet tool
still cannot approve anything, because it cannot sign. Once a workspace
registers its first approver, every dawn decision, release and recall must
carry approver signatures:

    request   anyone, including an agent over MCP, asks for a decision; the
              exact decision is stored in the workspace as a pending request
    approve   the person reviews it in a terminal (``brevet approve``),
              unlocks their key with the passphrase and signs it
    apply     with enough signatures (the named human, or the threshold of a
              mission group's members), Brevet applies the decision and
              records the signatures on the evidence chain

The approver register lives on the evidence chain as ``brevet.approver``
envelopes. The first approver registers themselves; every later change must
be signed by an approver already registered, and a new key must also sign its
own registration, proving its holder has it. No member acting alone can
weaken a mission group: changing its threshold, adding or removing a member,
or replacing a member's key needs the group's own quorum. An approver can
always withdraw their own key. ``verify_approvals`` replays the chain and
checks every signature against the register as it stood then.

Who holds each key is checked against an identity source the organisation
already trusts, when the manifest names one (``runtime_safety.approvals``):

    allowed_signers: <path>   an OpenSSH allowed-signers file, the format git
                              uses for signed commits, mapping each person's
                              email to their public keys (``BREVET_ALLOWED_SIGNERS``
                              names one too)
    github: true              each approver names their GitHub account, and the
                              key must be one of the keys GitHub publishes for it

An approver key can be the person's existing SSH key (``brevet approver add
--ssh-key``), so the key an organisation already verified is the one that
signs. A registration whose key the identity source does not list is
refused. Register at least two approvers, so a lost key can be revoked and
replaced by the other.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from brevet.canonical import Signer, canonical_json, object_sha256
from brevet.identity import require_identity
from brevet.models import _now, new_id
from brevet.workdir import file_lock

DECISION_KINDS = ("brevet.promotion", "brevet.release", "brevet.recall")
MIN_PASSPHRASE = 8
_REQUEST_FIELDS = {"request_id", "requested_at"}


# ------------------------------------------------------------- keys

def key_dir(directory: str | Path | None = None) -> Path:
    """Where approver keys live: outside any workspace, by default
    ~/.config/brevet/approvers (override with BREVET_APPROVER_DIR)."""
    if directory:
        return Path(directory).expanduser()
    env = os.environ.get("BREVET_APPROVER_DIR")
    return Path(env).expanduser() if env else Path.home() / ".config" / "brevet" / "approvers"


def _stem(identity: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", identity)


@dataclass
class ApproverKey:
    identity: str
    _key: Ed25519PrivateKey = field(repr=False)

    @property
    def public_key(self) -> str:
        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    def sign(self, payload: dict[str, Any]) -> str:
        return self._key.sign(canonical_json(payload).encode("utf-8")).hex()


def create_key(identity: str, passphrase: str,
               directory: str | Path | None = None) -> ApproverKey:
    """Create a passphrase-protected approver key for a person."""
    who = require_identity(identity, role="approver key holder")
    if not who.startswith("human:"):
        raise ValueError("approver keys belong to people: use a human:<who> identity, "
                         "and register it as a member of a mission group")
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise ValueError(f"choose a passphrase of at least {MIN_PASSPHRASE} characters")
    d = key_dir(directory)
    if not d.exists():
        d.mkdir(parents=True)
        try:
            d.chmod(0o700)
        except OSError:  # pragma: no cover
            pass
    pem_path = d / f"{_stem(who)}.pem"
    if pem_path.exists():
        raise FileExistsError(f"an approver key for {who} already exists at {pem_path}")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("utf-8")))
    fd = os.open(pem_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    approver = ApproverKey(who, key)
    (d / f"{_stem(who)}.json").write_text(json.dumps(
        {"identity": who, "public_key": approver.public_key, "created_at": _now()},
        indent=2), encoding="utf-8")
    return approver


def use_ssh_key(identity: str, ssh_key: str | Path,
                directory: str | Path | None = None) -> str:
    """Make a person's existing Ed25519 SSH key their approver key. Only the
    path is recorded; the private key stays where it is, under its own
    passphrase. Returns the public key."""
    who = require_identity(identity, role="approver key holder")
    if not who.startswith("human:"):
        raise ValueError("approver keys belong to people: use a human:<who> identity")
    path = Path(ssh_key).expanduser()
    pub = path.with_name(path.name + ".pub")
    if not path.exists():
        raise FileNotFoundError(f"no SSH key at {path}")
    if not pub.exists():
        raise FileNotFoundError(f"the public half of {path} ({pub}) is missing")
    public_key = ssh_to_hex(pub.read_text(encoding="utf-8"))
    d = key_dir(directory)
    d.mkdir(parents=True, exist_ok=True)
    meta = d / f"{_stem(who)}.json"
    if meta.exists() or (d / f"{_stem(who)}.pem").exists():
        raise FileExistsError(f"an approver key for {who} already exists in {d}")
    meta.write_text(json.dumps({"identity": who, "public_key": public_key,
                                "ssh_key": str(path.resolve()), "created_at": _now()},
                               indent=2), encoding="utf-8")
    return public_key


def load_key(identity: str, passphrase: str,
             directory: str | Path | None = None) -> ApproverKey:
    """Unlock a person's approver key with their passphrase."""
    who = require_identity(identity, role="approver key holder")
    d = key_dir(directory)
    pem_path = d / f"{_stem(who)}.pem"
    meta_path = d / f"{_stem(who)}.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if pem_path.exists():
        data, load = pem_path.read_bytes(), serialization.load_pem_private_key
    elif meta.get("ssh_key"):
        data, load = Path(meta["ssh_key"]).read_bytes(), serialization.load_ssh_private_key
    else:
        raise FileNotFoundError(f"no approver key for {who} in {d}")
    try:
        key = load(data, password=(passphrase or "").encode("utf-8") or None)
    except UnsupportedAlgorithm:
        raise RuntimeError('a passphrase-protected SSH key needs the bcrypt package: '
                           'pip install "brevet[ssh]"') from None
    except (ValueError, TypeError):
        raise PermissionError(f"wrong passphrase for {who}") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"the approver key for {who} is not an Ed25519 key")
    approver = ApproverKey(who, key)
    if meta.get("public_key") and meta["public_key"] != approver.public_key:
        raise PermissionError(f"the key for {who} no longer matches the one recorded")
    return approver


# ------------------------------------------------------------- identity

def ssh_to_hex(line: str) -> str:
    """The raw Ed25519 public key in an OpenSSH ``ssh-ed25519 AAAA...`` line."""
    parts = line.split()
    idx = next((i for i, p in enumerate(parts) if p == "ssh-ed25519"), None)
    if idx is None or idx + 1 >= len(parts):
        raise ValueError("not an ssh-ed25519 public key")
    blob = base64.b64decode(parts[idx + 1])
    (n,) = struct.unpack(">I", blob[:4])
    kind, rest = blob[4:4 + n], blob[4 + n:]
    (m,) = struct.unpack(">I", rest[:4])
    if kind != b"ssh-ed25519" or m != 32:
        raise ValueError("not an ssh-ed25519 public key")
    return rest[4:36].hex()


def hex_to_ssh(public_key: str) -> str:
    raw = bytes.fromhex(public_key)
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", len(raw)) + raw
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


_OPTION = re.compile(r'(cert-authority|(?:namespaces|valid-after|valid-before)="[^"]*"'
                     r'|(?:namespaces|valid-after|valid-before)=[^\s,"]*)')
_KEYTYPE = re.compile(r"^(ssh-|ecdsa-|sk-)")


def _signer_line(line: str) -> dict[str, Any] | None:
    """One allowed-signers entry: principals, options and the Ed25519 key."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    head = re.match(r'("([^"]*)"|\S+)\s+(.*)', line)
    if not head:
        return None
    principals, rest = (head.group(2) if head.group(2) is not None else head.group(1)), head.group(3)
    options: dict[str, str] = {}
    if not _KEYTYPE.match(rest):
        parts = re.match(r'((?:[^\s"]|"[^"]*")+)\s+(.*)', rest)
        if not parts:
            return None
        for item in re.findall(r'(?:[^,"]|"[^"]*")+', parts.group(1)):
            if not _OPTION.fullmatch(item):
                return None  # an option OpenSSH would not accept: the line vouches for nothing
            key, _, value = item.partition("=")
            options[key] = value.strip('"')
        rest = parts.group(2)
    if "cert-authority" in options:
        return None  # a certificate authority vouches for certificates, not this raw key
    tokens = rest.split()
    if len(tokens) < 2 or tokens[0] != "ssh-ed25519":
        return None
    return {"principals": [p for p in principals.split(",") if p],
            "key": ssh_to_hex(" ".join(tokens[:2])),
            "namespaces": options.get("namespaces"),
            "valid_after": options.get("valid-after"),
            "valid_before": options.get("valid-before")}


def allowed_signers(path: str | Path) -> list[dict[str, Any]]:
    """The Ed25519 entries of an OpenSSH allowed-signers file (the format git
    uses for signed commits). Malformed lines and certificate authorities are
    skipped."""
    out = []
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        try:
            entry = _signer_line(line)
        except (ValueError, struct.error, IndexError):
            continue
        if entry:
            out.append(entry)
    return out


def _ssh_time(text: str | None, *, end: bool) -> Any:
    """An allowed-signers timestamp: YYYYMMDD[HHMM[SS]], UTC with a Z."""
    from datetime import datetime, timezone
    if not text:
        return None
    utc = text.endswith(("Z", "z"))
    digits = text.rstrip("Zz")
    fmt = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}.get(len(digits))
    if fmt is None:
        raise ValueError(f"unreadable allowed-signers time {text!r}")
    zone = timezone.utc if utc else datetime.now().astimezone().tzinfo
    when = datetime.strptime(digits + "+0000", fmt + "%z").replace(tzinfo=zone)
    if len(digits) == 8 and end:
        when = when.replace(hour=23, minute=59, second=59)
    return when


def signer_listed(email: str, key: str, entries: list[dict[str, Any]]) -> bool:
    """Whether an entry lists this key for this email now: its principals
    match (``!`` negates), its namespaces allow Brevet (or git, whose file it
    may be) and its validity window holds."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for entry in entries:
        if entry["key"] != key:
            continue
        positive = [p for p in entry["principals"] if not p.startswith("!")]
        negative = [p[1:] for p in entry["principals"] if p.startswith("!")]
        if not any(fnmatch.fnmatchcase(email.lower(), p.lower()) for p in positive) \
                or any(fnmatch.fnmatchcase(email.lower(), p.lower()) for p in negative):
            continue
        spaces = entry.get("namespaces")
        if spaces is not None and not {"brevet", "git"} & {n.strip() for n in spaces.split(",")}:
            continue
        try:
            after = _ssh_time(entry.get("valid_after"), end=False)
            before = _ssh_time(entry.get("valid_before"), end=True)
        except ValueError:
            continue
        if (after and now < after) or (before and now > before):
            continue
        return True
    return False


def github_keys(user: str) -> set[str] | None:
    """The Ed25519 keys GitHub publishes for an account, or None when GitHub
    cannot be reached."""
    try:
        with urllib.request.urlopen(f"https://github.com/{user}.keys", timeout=10) as resp:
            text = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    keys = set()
    for line in text.splitlines():
        try:
            keys.add(ssh_to_hex(line))
        except ValueError:
            continue
    return keys


def github_member(org: str, user: str) -> bool | None:
    """Whether an account is a public member of a GitHub organisation, or
    None when GitHub cannot be reached."""
    url = f"https://api.github.com/orgs/{org}/public_members/{user}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status == 204
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def identity_policy(workdir: str | Path,
                    manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Where approver identities are checked. Sources outside the workspace
    come first: ``~/.config/brevet/approvals.yaml`` (or ``BREVET_CONFIG_DIR``)
    and ``BREVET_ALLOWED_SIGNERS``; then ``runtime_safety.approvals`` in the
    released manifest (the manifest on disk only before any release), found
    at ``manifest_path`` or, without one, beside the workspace folder. Every
    allowed-signers file named must list a key. A manifest that is named but
    missing, or cannot be read, stops the check rather than skipping it."""
    import yaml

    from brevet.anchor import config_dir
    from brevet.ledger import Ledger
    from brevet.releases import released_manifest
    files: list[str] = []
    policy: dict[str, Any] = {}

    def take(source: dict[str, Any]) -> None:
        if source.get("allowed_signers"):
            files.append(str(source["allowed_signers"]))
        for key in ("github", "github_org"):
            if source.get(key):
                policy[key] = source[key]

    user = config_dir() / "approvals.yaml"
    if user.exists():
        take(yaml.safe_load(user.read_text(encoding="utf-8")) or {})
    if os.environ.get("BREVET_ALLOWED_SIGNERS"):
        files.append(os.environ["BREVET_ALLOWED_SIGNERS"])
    wd = Path(workdir)
    if manifest_path is not None:
        mpath: Path | None = Path(manifest_path).expanduser()
        if not mpath.exists():
            raise PermissionError(f"cannot read the approver identity policy: the manifest "
                                  f"{mpath} does not exist")
    else:
        mpath = next((m for m in (wd.parent / "agent.yaml", wd / "agent.yaml") if m.exists()),
                     None)
    if mpath is not None:
        try:
            manifest, _ = released_manifest(wd, mpath, Ledger(wd / "ledger.jsonl",
                                                              anchoring=False))
        except (yaml.YAMLError, ValueError) as e:
            raise PermissionError(f"cannot read the approver identity policy: {e}") from None
        take(((manifest.runtime_safety or {}).get("approvals")) or {})
    if files:
        policy["allowed_signers"] = files
    return policy


def identity_problems(payload: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    """Why the identity source does not vouch for a new approver key."""
    who, key = payload.get("identity", ""), payload.get("public_key", "")
    email = who.split(":", 1)[1] if ":" in who else who
    problems = []
    files = policy.get("allowed_signers") or []
    for path in [files] if isinstance(files, str) else files:
        try:
            listed = signer_listed(email, key, allowed_signers(path))
        except OSError as e:
            problems.append(f"the allowed-signers file {path} cannot be read: {e}")
            continue
        if not listed:
            problems.append(f"the allowed-signers file {path} does not list this key for {email}")
    if policy.get("github") or policy.get("github_org"):
        user = payload.get("github")
        if not user:
            problems.append(f"{who} must name their GitHub account (--github)")
        else:
            keys = github_keys(user)
            if keys is None:
                problems.append("GitHub could not be reached to verify the key; try again")
            elif key not in keys:
                problems.append(f"GitHub does not publish this key for {user}")
            org = policy.get("github_org")
            if org and keys is not None:
                member = github_member(org, user)
                if member is None:
                    problems.append("GitHub could not be reached to check membership; try again")
                elif not member:
                    problems.append(f"{user} is not a public member of the {org} organisation")
    return problems


def identity_report(register: Register, policy: dict[str, Any]) -> dict[str, Any]:
    """Every active approver's key checked against the identity source,
    however it reached the register. ``unverified`` lists keys the source
    does not vouch for; ``unchecked`` lists checks that could not be made
    (GitHub unreachable)."""
    unverified, unchecked = [], []
    for who, entry in sorted(register.active().items()):
        for problem in identity_problems({"identity": who, "public_key": entry["public_key"],
                                          "github": entry.get("github")}, policy):
            (unchecked if "could not be reached" in problem else unverified).append(
                {"identity": who, "problem": problem})
    return {"approvers": len(register.active()), "unverified": unverified,
            "unchecked": unchecked}


def local_identities(directory: str | Path | None = None) -> list[str]:
    """Identities with an approver key on this machine."""
    d = key_dir(directory)
    if not d.exists():
        return []
    out = []
    for meta in d.glob("*.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if (meta.with_suffix(".pem").exists() or data.get("ssh_key")) and data.get("identity"):
            out.append(data["identity"])
    return sorted(out)


# ------------------------------------------------------------- payloads

def promotion_payload(capability_id: str, outcome: str, to_layer: str | None,
                      content_hash: str, approver: str, notes: str) -> dict[str, Any]:
    return {"kind": "brevet.promotion", "capability_id": capability_id, "outcome": outcome,
            "to_layer": to_layer, "content_hash": content_hash, "approver": approver,
            "notes": notes}


def release_payload(agent: str, from_version: str | None, to_version: str, channel: str,
                    lockfile_hash: str, delta_held_in: Any, delta_held_out: Any,
                    rationale: str, approver: str, manifest_hash: str,
                    evals: dict[str, Any] | None = None,
                    restores: str | None = None,
                    set_aside: list[str] | None = None) -> dict[str, Any]:
    return {"kind": "brevet.release", "agent": agent, "from_version": from_version,
            "to_version": to_version, "channel": channel, "lockfile_hash": lockfile_hash,
            "manifest_hash": manifest_hash, "delta_held_in": delta_held_in,
            "delta_held_out": delta_held_out, "evals": evals, "restores": restores,
            "set_aside": set_aside, "rationale": rationale, "approver": approver}


def evals_of(eval_summary: dict[str, Any] | None) -> dict[str, Any]:
    """What a release approval covers of its evidence: measured runs,
    attested deltas or a rollback."""
    es = eval_summary or {}
    return {"source": es.get("source"), "before": es.get("before"), "after": es.get("after")}


def recall_payload(capability_id: str, content_hash: str | None, reason: str,
                   reason_class: str, severity: str, action: str,
                   issued_by: str) -> dict[str, Any]:
    return {"kind": "brevet.recall", "capability_id": capability_id,
            "content_hash": content_hash, "reason": reason, "reason_class": reason_class,
            "severity": severity, "action": action, "issued_by": issued_by}


def expected_from_envelope(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """The payload a decision envelope's approval must have signed."""
    if kind == "brevet.promotion":
        return promotion_payload(body.get("capability_id"), body.get("outcome"),
                                 body.get("to_layer"), body.get("content_hash"),
                                 body.get("approver"), body.get("notes", ""))
    if kind == "brevet.release":
        es = body.get("eval_summary") or {}
        return release_payload(body.get("agent"), body.get("from_version"),
                               body.get("to_version"), body.get("channel"),
                               body.get("lockfile_hash"), es.get("delta_held_in"),
                               es.get("delta_held_out"), body.get("rationale") or "",
                               body.get("approved_by"), body.get("manifest_hash"),
                               evals_of(es), body.get("restores"), body.get("set_aside"))
    return recall_payload(body.get("capability_id"), body.get("content_hash"),
                          body.get("reason"), body.get("reason_class"), body.get("severity"),
                          body.get("action"), body.get("issued_by"))


def _decider(payload: dict[str, Any]) -> str:
    return payload.get("approver") or payload.get("issued_by") or ""


# ------------------------------------------------------------- register

@dataclass
class Register:
    """The approvers a workspace recognises, rebuilt from the evidence chain."""

    approvers: dict[str, dict[str, Any]] = field(default_factory=dict)
    thresholds: dict[str, int] = field(default_factory=dict)
    head: str | None = None  # chain hash of the last register change applied

    def active(self) -> dict[str, dict[str, Any]]:
        return {i: a for i, a in self.approvers.items() if a["active"]}

    @property
    def enabled(self) -> bool:
        """Signed approvals are required once any approver is registered."""
        return bool(self.active())

    def members(self, group: str) -> set[str]:
        return {i for i, a in self.active().items() if group in a["groups"]}

    def threshold(self, group: str) -> int:
        return self.thresholds.get(group, 1)

    def valid_signers(self, payload: dict[str, Any],
                      signatures: list[dict[str, Any]] | None) -> tuple[set[str], list[str]]:
        """Active approvers whose signatures over ``payload`` verify, and the
        problems with any signature that does not."""
        good: set[str] = set()
        problems: list[str] = []
        for sig in signatures or []:
            who = sig.get("identity")
            entry = self.active().get(who)
            if entry is None:
                problems.append(f"{who} is not an active approver")
            elif sig.get("public_key") != entry["public_key"]:
                problems.append(f"{who} signed with a key that is not registered")
            elif not Signer.verify(entry["public_key"], payload, sig.get("signature", "")):
                problems.append(f"the signature by {who} does not verify")
            else:
                good.add(who)
        return good, problems

    def _quorum(self, group: str, signers: set[str]) -> list[str]:
        """A mission group's consent to a change in it: its threshold of
        signatures from its members as they stand before the change (all of
        them, if fewer remain than the threshold)."""
        members = self.members(group)
        need = min(self.threshold(group), len(members))
        have = len(signers & members)
        if have < need:
            return [(f"changing {group} needs {need} signature(s) from its members; "
                     f"it has {have}")]
        return []

    def change_problems(self, record: dict[str, Any], *, history: bool = False) -> list[str]:
        """Check a register change against the register as it stands.

        No member acting alone can weaken a mission group: changing its
        threshold, adding or removing a member, or replacing a member's key
        needs the group's own quorum. An approver may always withdraw their
        own key or leave a group, since that only gives up authority.
        ``history`` replays a change recorded under 0.3.0 under the rules it
        was made with; replay sets it only for records older than the chain's
        first envelope written by 0.4.0 or later."""
        payload = record.get("payload") or {}
        if payload.get("kind") != "brevet.approver":
            return ["not a register change"]
        if record.get("payload_hash") != object_sha256(payload):
            return ["the payload hash does not match the payload"]
        if history and not payload.get("group_quorum"):
            return self._change_problems_0_3(payload, record.get("signatures") or [])
        problems = self._change_problems_current(payload, record.get("signatures") or [])
        if payload.get("after") != self.head:
            # signed for another state of the register, or on another chain
            problems.append("the approver register changed since this change was requested; "
                            "request it again")
        return problems

    def _change_problems_current(self, payload: dict[str, Any],
                                 sigs: list[dict[str, Any]]) -> list[str]:
        change, who = payload.get("change"), payload.get("identity")
        signers, problems = self.valid_signers(payload, sigs)
        if change == "add":
            possession = any(
                s.get("identity") == who and s.get("public_key") == payload.get("public_key")
                and Signer.verify(payload.get("public_key", ""), payload, s.get("signature", ""))
                for s in sigs)
            if not possession:
                return [f"the new key for {who} must sign its own registration"]
            if not self.enabled:
                return []  # the first approver registers themselves
            existing = self.active().get(who)
            before = set(existing["groups"]) if existing else set()
            after = set(payload.get("groups") or [])
            out: list[str] = []
            if existing is None:
                if not signers - {who}:
                    return [f"registering {who} needs the signature of another active approver"]
            elif payload.get("public_key") != existing["public_key"] and who not in signers:
                # a replacement key without the current one: the approver's
                # groups recover it, or another approver if it has none
                if before:
                    out += [p for g in sorted(before) for p in self._quorum(g, signers)]
                elif not signers - {who}:
                    return [(f"replacing the key of {who} needs its current key or the "
                             "signature of another active approver")]
            for group in sorted(after - before):
                if self.members(group):
                    out += self._quorum(group, signers)
                elif not signers - {who}:
                    out.append(f"creating {group} needs the signature of another active approver")
            if who not in signers:
                out += [p for g in sorted(before - after) for p in self._quorum(g, signers)]
            return out
        if change == "revoke":
            if who not in self.active():
                return [f"{who} is not an active approver"]
            if len(self.active()) == 1:
                return ["the last active approver cannot be revoked"]
            if not signers:
                return problems + ["a revocation needs an active approver's signature"]
            if who in signers:
                return []  # withdrawing one's own key
            return [p for g in sorted(self.active()[who]["groups"]) for p in self._quorum(g, signers)]
        if change == "threshold":
            group, count = payload.get("group"), payload.get("threshold")
            members = self.members(group or "")
            if not isinstance(count, int) or count < 1 or count > len(members):
                return [(f"{group} has {len(members)} active member(s); "
                         f"its threshold must be between 1 and that number")]
            if not signers & members:
                return problems + [f"a threshold change needs the signature of a member of {group}"]
            return self._quorum(group or "", signers)
        return [f"unknown register change {change!r}"]

    def _change_problems_0_3(self, payload: dict[str, Any],
                             sigs: list[dict[str, Any]]) -> list[str]:
        """The register rules of 0.3.0, for replaying changes made under it."""
        change, who = payload.get("change"), payload.get("identity")
        signers, problems = self.valid_signers(payload, sigs)
        if change == "add":
            possession = any(
                s.get("identity") == who and s.get("public_key") == payload.get("public_key")
                and Signer.verify(payload.get("public_key", ""), payload, s.get("signature", ""))
                for s in sigs)
            if not possession:
                return [f"the new key for {who} must sign its own registration"]
            if not self.enabled:
                return []
            existing = self.active().get(who)
            rotation = (existing is not None and
                        sorted(existing["groups"]) == sorted(payload.get("groups") or []))
            if not (rotation and signers) and not (signers - {who}):
                return [f"registering {who} needs the signature of another active approver"]
            return []
        if change == "revoke":
            if who not in self.active():
                return [f"{who} is not an active approver"]
            if len(self.active()) == 1:
                return ["the last active approver cannot be revoked"]
            return [] if signers else problems + ["a revocation needs an active approver's signature"]
        if change == "threshold":
            group, count = payload.get("group"), payload.get("threshold")
            members = self.members(group or "")
            if not isinstance(count, int) or count < 1 or count > len(members):
                return [(f"{group} has {len(members)} active member(s); "
                         f"its threshold must be between 1 and that number")]
            if not signers & members:
                return problems + [f"a threshold change needs the signature of a member of {group}"]
            return []
        return [f"unknown register change {change!r}"]

    def apply(self, record: dict[str, Any], chain_hash: str | None = None) -> None:
        payload = record["payload"]
        self.head = chain_hash
        if payload["change"] == "add":
            self.approvers[payload["identity"]] = {
                "public_key": payload["public_key"],
                "groups": sorted(payload.get("groups") or []), "active": True,
                "github": payload.get("github")}
        elif payload["change"] == "revoke":
            self.approvers[payload["identity"]]["active"] = False
        elif payload["change"] == "threshold":
            self.thresholds[payload["group"]] = payload["threshold"]

    def approval_problems(self, approval: dict[str, Any] | None,
                          expected: dict[str, Any], *, history: bool = False) -> list[str]:
        """Why ``approval`` does not authorise the decision ``expected``.
        ``history`` accepts a release approval signed under 0.3.0, before they
        covered the manifest; replay sets it only until the chain's first
        release approval made under 0.4.0."""
        if not isinstance(approval, dict):
            return ["the decision carries no approval"]
        payload = approval.get("payload") or {}
        if history and expected.get("kind") == "brevet.release" and "manifest_hash" not in payload:
            expected = {k: v for k, v in expected.items()
                        if k not in ("manifest_hash", "evals", "restores", "set_aside")}
        problems = []
        if approval.get("payload_hash") != object_sha256(payload):
            problems.append("the payload hash does not match the payload")
        differs = sorted({k for k, v in expected.items() if payload.get(k) != v}
                         | (set(payload) - set(expected) - _REQUEST_FIELDS))
        if differs:
            problems.append("the signed request does not match this decision "
                            f"({', '.join(differs)})")
        signers, sig_problems = self.valid_signers(payload, approval.get("signatures"))
        problems += sig_problems
        who = _decider(expected)
        if who.startswith("human:"):
            allowed, need = ({who} if who in self.active() else set()), 1
        else:
            allowed, need = self.members(who), self.threshold(who)
        if not allowed:
            problems.append(f"{who} has no active registered approver")
        elif len(signers & allowed) < need:
            problems.append(f"{who} needs {need} signature(s) from {', '.join(sorted(allowed))}; "
                            f"it has {len(signers & allowed)}")
        return problems


def register_from_chain(ledger) -> Register:
    """The register as the evidence chain defines it now; changes that fail
    their checks are ignored here and reported by ``verify_approvals``."""
    register = Register()
    used: set[str] = set()
    era = _Eras()
    for env in ledger.read():
        era.see(env)
        if env.get("kind") != "brevet.approver":
            continue
        body = env.get("body", {})
        rid = (body.get("payload") or {}).get("request_id")
        if rid in used:
            continue  # a replayed change never applies twice
        if not register.change_problems(body, history=era.register_history(body)):
            register.apply(body, env.get("chain_hash"))
            used.add(rid)
    return register


class _Eras:
    """Where a chain moved from 0.3.0's approval rules to 0.4.0's. Every
    envelope written since 0.4.0 names the runtime that wrote it; records
    older than the first such envelope (and than the first 0.4.0-style
    record of their type) replay under the rules they were made with. After
    that point only the current rules count, so a record in the old style
    cannot be appended later to slip past them."""

    def __init__(self) -> None:
        self.sealed = False
        self.quorum = False
        self.manifest = False

    def see(self, env: dict[str, Any]) -> None:
        if env.get("runtime"):
            self.sealed = True

    def register_history(self, body: dict[str, Any]) -> bool:
        if (body.get("payload") or {}).get("group_quorum"):
            self.quorum = True
        return not (self.quorum or self.sealed)

    def release_history(self, approval: dict[str, Any] | None) -> bool:
        if "manifest_hash" in ((approval or {}).get("payload") or {}):
            self.manifest = True
        return not (self.manifest or self.sealed)


def used_request_ids(ledger) -> set[str]:
    used = set()
    for env in ledger.read():
        body = env.get("body") or {}
        if env.get("kind") in DECISION_KINDS:
            rid = ((body.get("approval") or {}).get("payload") or {}).get("request_id")
        elif env.get("kind") == "brevet.approver":
            rid = (body.get("payload") or {}).get("request_id")
        else:
            continue
        if rid:
            used.add(rid)
    return used


def enforce(ledger, expected: dict[str, Any],
            approval: dict[str, Any] | None) -> dict[str, Any] | None:
    """Check the approval for a decision about to be applied.

    Without registered approvers, decisions stay unsigned. With them, a valid
    approval is required, and each signed request can be applied only once."""
    register = register_from_chain(ledger)
    if not register.enabled:
        if approval is not None:
            raise ValueError("no approvers are registered in this workspace, so it does "
                             "not take signed approvals; register one with "
                             "'brevet approver add'")
        return None
    if approval is None:
        raise PermissionError(
            "this workspace requires signed approvals: request the decision, then sign "
            "it in a terminal with 'brevet approve'")
    problems = register.approval_problems(approval, expected)
    rid = (approval.get("payload") or {}).get("request_id")
    if not rid:
        problems.append("the signed request has no request id")
    elif rid in used_request_ids(ledger):
        problems.append(f"request {rid} has already been applied")
    if problems:
        raise PermissionError("signed approval rejected: " + "; ".join(problems))
    return approval


def verify_approvals(ledger) -> dict[str, Any]:
    """Replay the chain and check every register change and every decision.

    Decisions taken before the first approver was registered count as
    unsigned; after that, each needs a valid approval used only once."""
    return verify_envelopes(ledger.read())


def verify_envelopes(envelopes) -> dict[str, Any]:
    """``verify_approvals`` over envelopes already read, in chain order."""
    register = Register()
    used: set[str] = set()
    era = _Eras()
    signed = unsigned = changes = 0
    invalid: list[dict[str, str]] = []
    for env in envelopes:
        era.see(env)
        kind, body = env.get("kind"), env.get("body") or {}
        if kind == "brevet.approver":
            problems = register.change_problems(body, history=era.register_history(body))
            rid = (body.get("payload") or {}).get("request_id")
            if rid in used:
                problems = [*problems, f"request {rid} was applied twice"]
            if problems:
                invalid.append({"envelope_id": env.get("envelope_id", ""),
                                "kind": kind, "problem": "; ".join(problems)})
                continue
            used.add(rid)
            register.apply(body, env.get("chain_hash"))
            changes += 1
        elif kind in DECISION_KINDS:
            approval = body.get("approval")
            if not register.enabled:
                if approval:
                    invalid.append({"envelope_id": env.get("envelope_id", ""), "kind": kind,
                                    "problem": "an approval recorded before any approver "
                                               "was registered"})
                else:
                    unsigned += 1
                continue
            history = era.release_history(approval) if kind == "brevet.release" else False
            problems = register.approval_problems(approval, expected_from_envelope(kind, body),
                                                  history=history)
            rid = ((approval or {}).get("payload") or {}).get("request_id")
            if rid and rid in used:
                problems.append(f"request {rid} was applied twice")
            if problems:
                invalid.append({"envelope_id": env.get("envelope_id", ""), "kind": kind,
                                "problem": "; ".join(problems)})
                continue
            used.add(rid)
            signed += 1
    return {"signing_required": register.enabled, "approvers": len(register.active()),
            "register_changes": changes, "signed": signed,
            "unsigned_before_signing": unsigned, "invalid": invalid}


# ------------------------------------------------------------- requests

class PendingRequests:
    """Requests awaiting signatures, one JSON line per version (latest wins)."""

    def __init__(self, workdir: str | Path):
        self.path = Path(workdir) / "approvals.jsonl"

    def _all(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                if isinstance(req, dict) and req.get("request_id"):
                    out[req["request_id"]] = req
        return out

    def save(self, request: dict[str, Any]) -> None:
        with file_lock(self.path), self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(request, ensure_ascii=False) + "\n")

    def get(self, request_id: str) -> dict[str, Any]:
        req = self._all().get(request_id)
        if req is None:
            raise KeyError(f"no approval request '{request_id}'")
        return req

    def pending(self) -> list[dict[str, Any]]:
        return [r for r in self._all().values() if r.get("status") == "pending"]


def create_request(workdir: str | Path, payload: dict[str, Any], *, summary: str,
                   manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Store a decision awaiting signatures. Asking again for the same
    decision returns the request already pending rather than a duplicate."""
    for existing in PendingRequests(workdir).pending():
        if {k: v for k, v in existing["payload"].items() if k not in _REQUEST_FIELDS} == payload:
            return existing
    rid = new_id("req")
    payload = {**payload, "request_id": rid, "requested_at": _now()}
    request = {"request_id": rid, "payload": payload, "payload_hash": object_sha256(payload),
               "signatures": [], "status": "pending", "summary": summary,
               "manifest_path": str(manifest_path) if manifest_path else None,
               "created_at": payload["requested_at"]}
    PendingRequests(workdir).save(request)
    return request


def sign_request(workdir: str | Path, request_id: str, key: ApproverKey) -> dict[str, Any]:
    """Add one person's signature to a pending request."""
    store = PendingRequests(workdir)
    req = store.get(request_id)
    if req.get("status") != "pending":
        raise ValueError(f"request {request_id} is {req.get('status')}, not pending")
    if any(s.get("identity") == key.identity for s in req["signatures"]):
        raise ValueError(f"{key.identity} has already signed request {request_id}")
    req["signatures"].append({"identity": key.identity, "public_key": key.public_key,
                              "signature": key.sign(req["payload"]), "signed_at": _now()})
    store.save(req)
    return req


def _approval(req: dict[str, Any]) -> dict[str, Any]:
    return {"payload": req["payload"], "payload_hash": req["payload_hash"],
            "signatures": req["signatures"]}


def request_register_add(workdir: str | Path, key: ApproverKey,
                         groups: list[str] | None = None,
                         github: str | None = None,
                         manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Ask to register (or rotate) an approver key; the key signs its own
    registration, so whoever approves it knows its holder has it. The
    manifest named here is where the identity policy is read when the
    registration is applied."""
    groups = sorted({require_identity(g, role="group", mission_group=True)
                     for g in (groups or [])})
    payload = {"kind": "brevet.approver", "change": "add", "identity": key.identity,
               "public_key": key.public_key, "groups": groups, "group_quorum": True,
               "after": _register_head(workdir)}
    if github:
        payload["github"] = github
    req = create_request(workdir, payload, manifest_path=manifest_path,
                         summary=f"register {key.identity}"
                                 + (f" in {', '.join(groups)}" if groups else ""))
    return sign_request(workdir, req["request_id"], key)


def _register_head(workdir: str | Path) -> str | None:
    """The state of the register a change is requested for: the chain hash of
    the last register change applied. A signed change applies only to that
    state, so a request made earlier, or on another chain, cannot be replayed
    once the register has moved on."""
    from brevet.ledger import Ledger
    return register_from_chain(Ledger(Path(workdir) / "ledger.jsonl", anchoring=False)).head


def request_register_revoke(workdir: str | Path, identity: str) -> dict[str, Any]:
    who = require_identity(identity, role="approver")
    return create_request(workdir, {"kind": "brevet.approver", "change": "revoke",
                                    "identity": who, "group_quorum": True,
                                    "after": _register_head(workdir)},
                          summary=f"revoke {who}")


def request_threshold(workdir: str | Path, group: str, count: int) -> dict[str, Any]:
    g = require_identity(group, role="group", mission_group=True)
    return create_request(workdir, {"kind": "brevet.approver", "change": "threshold",
                                    "group": g, "threshold": int(count), "group_quorum": True,
                                    "after": _register_head(workdir)},
                          summary=f"require {count} signature(s) for {g}")


def request_promotion(workdir: str | Path, store, capability_id: str, outcome: str, *,
                      approver: str, to_layer: str = "advisory", notes: str = "",
                      manifest_path: str | Path | None = None) -> dict[str, Any]:
    from brevet.lifecycle import prepare_promotion
    cap, layer, who, payload = prepare_promotion(
        store, capability_id, outcome, approver=approver, to_layer=to_layer, notes=notes)
    detail = f" to {layer.value}" if outcome == "promote" else ""
    return create_request(workdir, payload, manifest_path=manifest_path,
                          summary=f"{outcome} {capability_id}{detail} as {who}: {cap.title}")


def request_release(workdir: str | Path, manifest, store, *, to_version: str, channel: str,
                    approver: str, eval_summary: dict[str, Any] | None = None,
                    rationale: str = "", manifest_path: str | Path | None = None,
                    harness: tuple[list, list[str]] | None = None) -> dict[str, Any]:
    """Ask for a release. The request keeps the harness inventory it was made
    with, so the release ships exactly what was signed."""
    from brevet.ledger import Ledger
    from brevet.lifecycle import prepare_release
    who, chan, lock, payload = prepare_release(
        manifest, store, to_version=to_version, channel=channel, approver=approver,
        eval_summary=eval_summary, rationale=rationale, harness=harness,
        ledger=Ledger(Path(workdir) / "ledger.jsonl"))
    ids = [r.capability_id for r in lock.resolved]
    named = ", ".join(ids[:8]) + (f" and {len(ids) - 8} more" if len(ids) > 8 else "")
    parts = f"{len(ids)} capabilities" + (f" ({named})" if ids else "")
    if lock.harness:
        parts += f" and {len(lock.harness)} harness components"
    source = (eval_summary or {}).get("source", "attested")
    evidence = ("eval runs " + f"{eval_summary['before']} -> {eval_summary['after']}"
                if source == "measured" else "deltas attested by the approver")
    req = create_request(
        workdir, payload, manifest_path=manifest_path,
        summary=f"release {manifest.agent} {manifest.version} -> {to_version} on the "
                f"{chan.value} channel as {who}, locking {parts}; evidence: {evidence}")
    changed = False
    if harness is not None and "harness" not in req:
        req["harness"] = {"components": [c.model_dump() for c in harness[0]],
                          "sources": list(harness[1])}
        changed = True
    if eval_summary is not None and "eval_summary" not in req:
        req["eval_summary"] = dict(eval_summary)
        changed = True
    if changed:
        PendingRequests(workdir).save(req)
    return req


def request_rollback(workdir: str | Path, manifest, store, *, target: str, approver: str,
                     as_version: str | None = None, channel: str | None = None,
                     rationale: str = "", remove_added: bool = False,
                     manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Ask to roll back to an earlier release (a release that names the
    version it restores). The summary names every file it restores or sets
    aside, so the approver sees exactly what changes on disk."""
    from brevet.ledger import Ledger
    from brevet.releases import prepare_rollback
    wd = Path(workdir)
    plan = prepare_rollback(wd, Ledger(wd / "ledger.jsonl"), store,
                            Path(manifest_path) if manifest_path else None, manifest,
                            target=target, approver=approver, as_version=as_version,
                            channel=channel, rationale=rationale, remove_added=remove_added)
    left_out = (f"; leaving out {', '.join(d['capability_id'] for d in plan['dropped'])}"
                if plan["dropped"] else "")
    files = ", ".join(r["label"] for r in plan["restore"]) or "none"
    aside = f"; setting aside {', '.join(plan['added'])}" if remove_added and plan["added"] else ""
    req = create_request(
        workdir, plan["payload"], manifest_path=manifest_path,
        summary=f"roll {manifest.agent} back to release {target} as {plan['version']} on the "
                f"{plan['channel']} channel as {plan['approver']}, locking "
                f"{len(plan['lock'].resolved)} capabilities and restoring files: "
                f"{files}{aside}{left_out}")
    if remove_added and not req.get("remove_added"):
        req["remove_added"] = True
        PendingRequests(workdir).save(req)
    return req


def request_recall(workdir: str | Path, store, capability_id: str, *, reason: str,
                   issued_by: str, reason_class: str = "incorrect", severity: str = "high",
                   action: str = "rollback",
                   manifest_path: str | Path | None = None) -> dict[str, Any]:
    from brevet.lifecycle import prepare_recall
    cap, who, payload = prepare_recall(
        store, capability_id, reason=reason, reason_class=reason_class, severity=severity,
        issued_by=issued_by, action=action)
    return create_request(workdir, payload, manifest_path=manifest_path,
                          summary=f"recall {capability_id} as {who}: {cap.title}")


def apply_if_ready(workdir: str | Path, request_id: str, *,
                   manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Apply a request once its signatures suffice; otherwise say what it needs."""
    wd = Path(workdir)
    store_reqs = PendingRequests(wd)
    req = store_reqs.get(request_id)
    if req.get("status") != "pending":
        return {"request_id": request_id, "status": req.get("status"),
                "result": req.get("result")}
    try:
        return _apply(wd, store_reqs, req, manifest_path)
    except (PermissionError, ValueError, KeyError, FileNotFoundError) as exc:
        # A request that cannot be applied as signed (its decision changed,
        # or a signature is bad) is closed, so it does not linger as pending.
        req["status"], req["error"] = "failed", str(exc.args[0] if exc.args else exc)
        store_reqs.save(req)
        raise


def _apply(wd: Path, store_reqs: PendingRequests, req: dict[str, Any],
           manifest_path: str | Path | None) -> dict[str, Any]:
    import yaml

    from brevet import lifecycle
    from brevet.ledger import Ledger
    from brevet.models import AgentManifest, ReleaseRecord

    request_id = req["request_id"]
    ledger = Ledger(wd / "ledger.jsonl")
    register = register_from_chain(ledger)
    payload, approval = req["payload"], _approval(req)
    kind = payload.get("kind")

    if kind == "brevet.approver":
        problems = register.change_problems(approval)
        if payload.get("change") == "add" and not problems:
            problems = identity_problems(
                payload, identity_policy(wd, manifest_path or req.get("manifest_path")))
        if problems:
            if all(" needs " in p for p in problems):
                return {"request_id": request_id, "status": "pending", "needs": problems}
            raise PermissionError("register change rejected: " + "; ".join(problems))
        ledger.append("brevet.approver", approval)
        result: dict[str, Any] = {"change": payload["change"],
                                  "identity": payload.get("identity") or payload.get("group")}
    else:
        expected = {k: v for k, v in payload.items() if k not in _REQUEST_FIELDS}
        problems = register.approval_problems(approval, expected)
        if problems:
            if all("needs" in p and "signature(s)" in p for p in problems):
                return {"request_id": request_id, "status": "pending", "needs": problems}
            raise PermissionError("signed approval rejected: " + "; ".join(problems))
        store = lifecycle.CapabilityStore(wd / "capabilities.jsonl")
        if kind == "brevet.promotion":
            cap = lifecycle.dawn_decide(
                store, ledger, payload["capability_id"], payload["outcome"],
                approver=payload["approver"], to_layer=payload.get("to_layer") or "advisory",
                notes=payload.get("notes", ""), approval=approval)
            result = {"capability_id": cap.capability_id,
                      "validation_state": cap.validation_state.value,
                      "authority_layer": cap.authority_layer.value}
        elif kind == "brevet.release" and payload.get("restores"):
            from brevet.releases import rollback

            mpath = Path(manifest_path or req.get("manifest_path") or "agent.yaml")
            manifest = AgentManifest(**yaml.safe_load(mpath.read_text(encoding="utf-8")))
            manifest, lock, record, plan = rollback(
                wd, ledger, store, Signer(wd / "keys" / "brevet_ed25519.pem"), mpath, manifest,
                target=payload["restores"], approver=payload["approver"],
                as_version=payload["to_version"], channel=payload["channel"],
                rationale=payload.get("rationale", ""), approval=approval,
                remove_added=bool(req.get("remove_added")))
            result = {"from": record.from_version, "to": record.to_version,
                      "restores": payload["restores"], "channel": record.channel.value,
                      "locked": len(lock.resolved), "restored_files": plan["restored_files"]}
        elif kind == "brevet.release":
            from brevet.harness import compare, file_components
            from brevet.models import HarnessComponent
            from brevet.releases import publish

            mpath = Path(manifest_path or req.get("manifest_path") or "agent.yaml")
            manifest = AgentManifest(**yaml.safe_load(mpath.read_text(encoding="utf-8")))
            harness = None
            if req.get("harness"):
                locked = [HarnessComponent(**c) for c in req["harness"]["components"]]
                sources = req["harness"]["sources"]
                if "files" in sources:
                    live = file_components(manifest, mpath.parent,
                                           exclude=[mpath, mpath.parent / "capabilities.lock"],
                                           exclude_dirs=[wd])
                    changed = compare([c for c in locked if c.kind == "file"], live)
                    if changed:
                        raise PermissionError(
                            "harness files changed since the release was requested: "
                            + ", ".join(c["component_id"] for c in changed))
                harness = (locked, sources)
            summary = req.get("eval_summary") or {
                "source": (payload.get("evals") or {}).get("source") or "attested",
                "delta_held_in": payload["delta_held_in"],
                "delta_held_out": payload["delta_held_out"], "gate": "conservative"}
            manifest, lock, record = lifecycle.release(
                manifest, store, ledger, Signer(wd / "keys" / "brevet_ed25519.pem"),
                to_version=payload["to_version"], channel=payload["channel"],
                approver=payload["approver"], eval_summary=summary,
                rationale=payload.get("rationale", ""), approval=approval, harness=harness)
            publish(wd, manifest, lock, mpath)
            result = {"from": record.from_version, "to": record.to_version,
                      "channel": record.channel.value, "locked": len(lock.resolved)}
        elif kind == "brevet.recall":
            releases = [ReleaseRecord(**e["body"]) for e in ledger.read("brevet.release")]
            notice = lifecycle.recall(
                store, ledger, payload["capability_id"], reason=payload["reason"],
                reason_class=payload["reason_class"], severity=payload["severity"],
                issued_by=payload["issued_by"], releases=releases,
                action=payload["action"], approval=approval)
            result = {"recall_id": notice.recall_id,
                      "affected_releases": [r["version"] for r in notice.affected_releases]}
        else:
            raise ValueError(f"unknown request kind {kind!r}")
    req["status"], req["result"] = "applied", result
    store_reqs.save(req)
    return {"request_id": request_id, "status": "applied", "result": result}
