"""Write README_PYPI.md, the README as PyPI shows it.

PyPI cannot resolve links relative to the repository and drops the
<picture> element, so this script pins every figure and link in README.md
to the release tag on GitHub and keeps the light version of each figure.
The release tag must be pushed before the package is uploaded, or the
figures on PyPI will not load.

    python scripts/make_pypi_readme.py          # write README_PYPI.md
    python scripts/make_pypi_readme.py --check  # exit 1 if it is out of date
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "BrightbeamAI/brevet"
HEADER = ("<!-- Generated from README.md by scripts/make_pypi_readme.py for PyPI. "
          "Edit README.md, then run the script. -->\n")


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("pyproject.toml has no version")
    return match[1]


def _absolute(url: str, base: str) -> str:
    if re.match(r"^(https?:|mailto:|#)", url):
        return url
    return base + url.removeprefix("./")


def render(readme: str, version: str) -> str:
    tag = f"v{version}"
    raw = f"https://raw.githubusercontent.com/{REPO}/{tag}/"
    blob = f"https://github.com/{REPO}/blob/{tag}/"
    parts = re.split(r"(^```.*?^```[ \t]*$)", readme, flags=re.MULTILINE | re.DOTALL)
    out = []
    for i, part in enumerate(parts):
        if i % 2:  # fenced code: leave untouched
            out.append(part)
            continue
        part = re.sub(r'[ \t]*<source media="\(prefers-color-scheme: dark\)"[^>]*>\n', "", part)
        part = re.sub(r"[ \t]*</?picture>\n", "", part)
        part = re.sub(r'(src|srcset)="([^"]+)"',
                      lambda m: f'{m[1]}="{_absolute(m[2], raw)}"', part)
        part = re.sub(r'href="([^"]+)"', lambda m: f'href="{_absolute(m[1], blob)}"', part)
        part = re.sub(r"\]\(([^)\s]+)\)", lambda m: f"]({_absolute(m[1], blob)})", part)
        out.append(part)
    return HEADER + "".join(out)


def main(argv: list[str]) -> int:
    expected = render((ROOT / "README.md").read_text(encoding="utf-8"), project_version())
    target = ROOT / "README_PYPI.md"
    if "--check" in argv:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != expected:
            print("README_PYPI.md is out of date: run python scripts/make_pypi_readme.py")
            return 1
        return 0
    target.write_text(expected, encoding="utf-8")
    print(f"wrote {target.name} for v{project_version()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
