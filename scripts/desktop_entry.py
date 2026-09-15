"""Frozen-app entry point.

A packaged build has no working directory to speak of and no ``.env`` beside
the source tree, so data goes to the user's own application-data directory
unless they have said otherwise.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "NFLPicker"


def main() -> int:
    os.environ.setdefault("NFLPICKER_DATA_DIR", str(default_data_dir()))
    if getattr(sys, "frozen", False):
        # Bundled resources live beside the executable at runtime.
        os.environ.setdefault("NFLPICKER_BUNDLE", str(Path(sys._MEIPASS)))

    from nflpicker.desktop import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
