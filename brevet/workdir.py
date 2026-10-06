"""Where a Brevet workspace lives, and safe access to its files.

A workspace is a folder (``.brevet/`` by default) that holds the evidence
chain, the capability store and the signing key. This module creates it
private to the user with a ``.gitignore`` that keeps it out of version
control, resolves its location for the MCP server, and serialises writes
from concurrent processes so two writers cannot break the evidence chain.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

GITIGNORE = (
    "# Created by Brevet. This folder holds a private signing key and the\n"
    "# evidence of real work, so it stays out of version control.\n"
    "*\n"
)


def ensure_workdir(path: str | Path) -> Path:
    """Create the workspace folder if needed, readable only by its owner,
    with a .gitignore that keeps it out of version control."""
    p = Path(path).expanduser()
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
        try:
            p.chmod(0o700)
        except OSError:  # pragma: no cover - filesystems without POSIX modes
            pass
    gitignore = p / ".gitignore"
    if not gitignore.exists():
        try:
            gitignore.write_text(GITIGNORE, encoding="utf-8")
        except OSError:  # pragma: no cover
            pass
    return p


def resolve_workspace(workdir: str | None = None,
                      manifest_path: str | None = None) -> tuple[Path, Path]:
    """The workspace folder and manifest path to use.

    Explicit arguments win; then ``BREVET_WORKDIR`` and ``BREVET_MANIFEST``;
    then ``BREVET_HOME``, a folder holding ``agent.yaml`` and ``.brevet/``;
    then the current directory. MCP clients may start servers from any
    directory, including ``/``, so a server should be given one of these.
    """
    home = os.environ.get("BREVET_HOME")
    base = Path(home).expanduser() if home else Path(".")
    wd = workdir or os.environ.get("BREVET_WORKDIR") or str(base / ".brevet")
    mp = manifest_path or os.environ.get("BREVET_MANIFEST") or str(base / "agent.yaml")
    return Path(wd).expanduser(), Path(mp).expanduser()


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Hold an exclusive lock on ``<path>.lock`` for the duration of the
    block, so processes appending to the same file take turns."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as fh:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def write_atomic(path: str | Path, text: str) -> None:
    """Replace a file in one step, so a process reading it at the same moment
    sees the old version or the new one, never half of each."""
    target = Path(path)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
