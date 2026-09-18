"""Which build this is.

Written because there was no way to answer the only question that matters when
something is reported broken: is this the build that has the fix. Without it a
report and a fix can pass each other indefinitely -- the app looks identical,
so "still broken" and "already fixed" are both unfalsifiable.

The build stamps `_build.py` as it packages. A source checkout has no such
file and falls back to git, and something that is neither says so rather than
guessing.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path


def _stamped() -> dict | None:
    try:
        from . import _build  # type: ignore
    except Exception:                                         # noqa: BLE001
        return None
    return {"commit": getattr(_build, "COMMIT", "")[:7],
            "built_at": getattr(_build, "BUILT_AT", ""),
            "source": "release"}


def _from_git() -> dict | None:
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%h %cI"],
            capture_output=True, text=True, timeout=5, check=True).stdout.split()
    except Exception:                                         # noqa: BLE001
        return None
    if not out:
        return None
    return {"commit": out[0], "built_at": out[1] if len(out) > 1 else "",
            "source": "checkout"}


@lru_cache(maxsize=1)
def build_info() -> dict:
    """Commit, build time and where that came from."""
    return _stamped() or _from_git() or {
        "commit": "unknown", "built_at": "", "source": "unknown"}


def label() -> str:
    """One short string for the corner of the window."""
    info = build_info()
    if info["commit"] == "unknown":
        return "build unknown"
    day = (info["built_at"] or "")[:10]
    return f"build {info['commit']}{f' · {day}' if day else ''}"
