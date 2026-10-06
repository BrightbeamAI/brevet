"""Canonical JSON, hashing and signing.

Canonical JSON here means sorted keys, compact separators and UTF-8, as
Python's json module writes them. Strings and integers encode as RFC 8785
specifies; floats follow Python's repr, which differs from RFC 8785 for very
small and very large values, so hashes are reproducible with Brevet's own
encoder. Content is addressed by SHA-256, releases are signed with Ed25519,
and the evidence chain links envelopes as
sha256(canonical(envelope) || prev_hash).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_sha256(content: str) -> str:
    return "sha256:" + sha256_hex(content.encode("utf-8"))


def object_sha256(obj: Any) -> str:
    return "sha256:" + sha256_hex(canonical_json(obj).encode("utf-8"))


def chain_hash(envelope: dict[str, Any], prev_hash: str) -> str:
    payload = canonical_json(envelope).encode("utf-8") + prev_hash.encode("utf-8")
    return "sha256:" + sha256_hex(payload)


class Signer:
    """Ed25519 signer with one keypair per workspace.

    The private key is created on first use (the first release), written
    readable only by its owner, and never leaves the workspace. Reading,
    verifying and reporting never create a key."""

    def __init__(self, key_path: Path | str):
        self.key_path = Path(key_path)
        self._key: Ed25519PrivateKey | None = None

    @property
    def has_key(self) -> bool:
        return self._key is not None or self.key_path.exists()

    def _private_key(self) -> Ed25519PrivateKey:
        if self._key is None:
            if self.key_path.exists():
                self._key = serialization.load_pem_private_key(
                    self.key_path.read_bytes(), password=None
                )
            else:
                key = Ed25519PrivateKey.generate()
                if not self.key_path.parent.exists():
                    self.key_path.parent.mkdir(parents=True)
                    try:
                        self.key_path.parent.chmod(0o700)
                    except OSError:  # pragma: no cover
                        pass
                pem = key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(pem)
                self._key = key
        return self._key

    def sign(self, obj: Any) -> str:
        sig = self._private_key().sign(canonical_json(obj).encode("utf-8"))
        return sig.hex()

    def public_key_hex(self) -> str:
        pub = self._private_key().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return pub.hex()

    @staticmethod
    def verify(public_key_hex: str, obj: Any, signature_hex: str) -> bool:
        try:
            pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
            pub.verify(bytes.fromhex(signature_hex), canonical_json(obj).encode("utf-8"))
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False
