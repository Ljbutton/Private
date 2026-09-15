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


def selftest() -> int:
    """Start the server, answer one request, exit.

    This exists for the packaged build. PyInstaller exiting 0 says the bundle
    was written, not that it works: the usual failure is a hidden import that
    was never collected, and that is invisible until someone double-clicks the
    icon. Booting the frozen binary once in CI turns that into a red build.
    """
    from nflpicker.desktop import ServerThread

    server = ServerThread()
    try:
        url = server.start()
        import httpx

        response = httpx.get(f"{url}/api/state", timeout=15.0)
        response.raise_for_status()
        payload = response.json()
    finally:
        server.stop()

    print(f"selftest ok: {url}/api/state -> {len(payload)} keys", flush=True)
    return 0


def main() -> int:
    os.environ.setdefault("NFLPICKER_DATA_DIR", str(default_data_dir()))
    if getattr(sys, "frozen", False):
        # Bundled resources live beside the executable at runtime.
        os.environ.setdefault("NFLPICKER_BUNDLE", str(Path(sys._MEIPASS)))

    if "--selftest" in sys.argv:
        return selftest()

    from nflpicker.desktop import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
