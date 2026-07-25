"""Model assistance: local-first, drafts only, always logged.

A model may help the dream cycle word an intervention or summarise a cluster.
It may never promote, validate, reject, revoke, or retrieve anything (the
Metis rule, inherited verbatim). Every call is recorded as a
``brevet.model_assist`` envelope with ``human_review_required: true``.

Default is ``NoModelAssist`` (deterministic templates; zero network), so the
whole runtime works with no model installed. ``OllamaAssist`` talks to a
local Ollama server with stdlib urllib, no extra dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol


class ModelAssist(Protocol):
    provider: str
    model: str

    def draft(self, prompt: str) -> str | None:
        """Return a draft, or None to fall back to the deterministic template."""
        ...


class NoModelAssist:
    provider = "none"
    model = "none"

    def draft(self, prompt: str) -> str | None:
        return None


class OllamaAssist:
    """Local model drafting via Ollama (default gemma4). Fails soft: any
    error returns None and the deterministic template is used instead."""

    provider = "ollama"

    def __init__(self, model: str = "gemma4:12b",
                 host: str = "http://localhost:11434", timeout: float = 60.0):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    def draft(self, prompt: str) -> str | None:
        body = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/generate", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = (data.get("response") or "").strip()
            return text or None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ConnectionError, json.JSONDecodeError, OSError):
            return None


def log_assist(ledger, assist: ModelAssist, *, purpose: str, prompt: str,
               output: str | None, used: bool) -> str | None:
    """Record a model-assist event. No-op for NoModelAssist non-calls."""
    if assist.provider == "none":
        return None
    return ledger.append("brevet.model_assist", {
        "provider": assist.provider,
        "model": assist.model,
        "purpose": purpose,
        "prompt": prompt[:2000],
        "output": (output or "")[:4000],
        "output_status": "draft",
        "used": used,
        "human_review_required": True,
    })


def from_name(name: str, model: str | None = None) -> ModelAssist:
    if name in ("none", "off", ""):
        return NoModelAssist()
    if name == "ollama":
        return OllamaAssist(model=model or "gemma4:12b")
    raise ValueError(f"unknown assist provider '{name}' (use: none, ollama)")
