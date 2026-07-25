"""Canonicalisation, hashing, and signing.

Mirrors CHAP's approach: JCS-style canonical JSON (sorted keys, no
insignificant whitespace, UTF-8), sha256 content addressing, Ed25519
signatures. The ledger chains envelopes as sha256(canonical(envelope) || prev_hash).
"""

from __future__ import annotations

import hashlib
import json
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
    """Ed25519 signer with on-disk PEM keys (one keypair per workspace)."""

    def __init__(self, key_path: Path):
        self.key_path = Path(key_path)
        if self.key_path.exists():
            self._key = serialization.load_pem_private_key(
                self.key_path.read_bytes(), password=None
            )
        else:
            self._key = Ed25519PrivateKey.generate()
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(
                self._key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )

    def sign(self, obj: Any) -> str:
        sig = self._key.sign(canonical_json(obj).encode("utf-8"))
        return sig.hex()

    def public_key_hex(self) -> str:
        pub = self._key.public_key().public_bytes(
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
