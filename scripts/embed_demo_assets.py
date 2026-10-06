"""Embed the current diagrams into docs/demo.html.

docs/demo.html must open as a single file, so its figures are data URIs.
Each figure is written ``<img data-asset="NAME-light" ...>`` (or ``-dark``);
this script refreshes those images from docs/assets/NAME-light.svg and
NAME-dark.svg. make_diagrams.py runs it after regenerating the diagrams.

    python scripts/embed_demo_assets.py          # update docs/demo.html
    python scripts/embed_demo_assets.py --check  # exit 1 if it is out of date
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "docs" / "demo.html"
ASSETS = ROOT / "docs" / "assets"
_IMG = re.compile(r'(<img\b[^>]*\bdata-asset="([a-z0-9-]+)"[^>]*\bsrc=")([^"]*)(")')


def data_uri(name: str) -> str:
    svg = (ASSETS / f"{name}.svg").read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def render(html: str) -> str:
    return _IMG.sub(lambda m: m[1] + data_uri(m[2]) + m[4], html)


def main(argv: list[str] | None = None) -> int:
    argv = argv or []
    html = DEMO.read_text(encoding="utf-8")
    updated = render(html)
    if "--check" in argv:
        if updated != html:
            print("docs/demo.html embeds outdated diagrams: run python scripts/embed_demo_assets.py")
            return 1
        return 0
    DEMO.write_text(updated, encoding="utf-8")
    print(f"embedded {len(_IMG.findall(html))} figures in {DEMO.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
