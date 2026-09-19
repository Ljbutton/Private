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
    """Where the app keeps its data.

    "TheEdge" for a new install; an existing "NFLPicker" directory is used as
    it stands. The app was renamed, the picks were not -- and the safe way to
    carry a database across a rename is to keep reading it where it already
    is, not to move it on first launch and hope the copy survived. Nothing is
    ever written to the old location that a new install would look for.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    for name in ("TheEdge", "NFLPicker"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return base / "TheEdge"


def ensure_stdio(data_dir: Path) -> None:
    """Give the process a real stdout, because a windowed build has none.

    PyInstaller builds this app with ``console=False`` so no terminal sits
    behind the window, and on Windows that leaves ``sys.stdout`` and
    ``sys.stderr`` set to **None** -- not a closed file, not a null sink,
    None. Any library that touches them raises AttributeError.

    uvicorn is one such library. Its default log formatter decides on colour
    with ``sys.stdout.isatty()``, so configuring logging raised
    ``AttributeError: 'NoneType' object has no attribute 'isatty'``, which
    ``logging.config`` reports as "Unable to configure formatter 'default'".
    That happened inside ``uvicorn.Config(...)``, before the server ever
    started, so the app exited without a window.

    It hid from every test for one reason: capturing a subprocess's output
    *gives it* a real stdout. Running ``--selftest`` with its output
    redirected -- which is the only way to read it, and what CI does -- was
    enough to make the failure disappear. The bug only exists when nothing is
    listening, which is exactly how a user launches the app.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    try:
        logs = data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stream = open(logs / "console.log", "a", encoding="utf-8", buffering=1)
    except Exception:                                     # noqa: BLE001
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except Exception:                                 # noqa: BLE001
            return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


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

    # Whether the webview backend was collected at all. A packaged app that
    # cannot import it still runs -- it falls back to a browser tab -- so this
    # failure is invisible at runtime and silently undoes the entire point of
    # shipping a native app. Importing is the right level of check: it tests
    # what is in the bundle, not whether the CI runner has a window server.
    try:
        import webview  # noqa: F401
    except Exception as exc:                              # noqa: BLE001
        print(f"selftest FAILED: webview did not import: {exc!r}", flush=True)
        return 1

    # Importing pywebview was never enough. The window opens through
    # webview.guilib, and the package shadows that submodule with a None
    # attribute until webview.start() runs -- so resolving it wrongly made the
    # app fall back to a browser tab on every platform while this check stayed
    # green. Resolving the module and finding initialize() on it is the part
    # that actually failed, and it needs no display to verify.
    try:
        from importlib import import_module

        guilib = import_module("webview.guilib")
        if not callable(getattr(guilib, "initialize", None)):
            raise AttributeError("webview.guilib has no initialize()")
    except Exception as exc:                              # noqa: BLE001
        print(f"selftest FAILED: webview backend not resolvable: {exc!r}", flush=True)
        return 1

    # Resolvable is not the same as usable, and the gap between them shipped a
    # Windows build that exited 0 without ever opening a window. `initialize()`
    # was only checked for being *callable*; actually calling it is what picks
    # and imports a backend, and that is where the failure was.
    from nflpicker import desktop

    if sys.platform == "win32":
        # The precise failure: edgechromium imports its WebView2 assemblies at
        # module scope, so a bundle missing pywebview's lib/ raises here. Left
        # unchecked, pywebview falls back to the legacy MSHTML backend, which
        # resolves fine and then opens nothing.
        try:
            import webview.platforms.edgechromium  # noqa: F401
        except Exception as exc:                          # noqa: BLE001
            print(f"selftest FAILED: the Edge WebView2 backend did not import: "
                  f"{exc!r}", flush=True)
            return 1

    # Only Windows treats this as fatal. A CI runner is not a desk: a Mac build
    # agent can lack a usable window server for reasons that say nothing about
    # the machine the app will actually run on, and failing the build there
    # would be noise. The Windows agent has Edge, so a False there is the bug.
    usable = desktop.available()
    if not usable and sys.platform == "win32":
        print("selftest FAILED: no usable webview renderer; the packaged app "
              "would open no window", flush=True)
        return 1

    print(f"selftest ok: {url}/api/state -> {len(payload)} keys, "
          f"webview renderer usable={usable}", flush=True)
    return 0


def main() -> int:
    os.environ.setdefault("NFLPICKER_DATA_DIR", str(default_data_dir()))
    if getattr(sys, "frozen", False):
        # Bundled resources live beside the executable at runtime.
        os.environ.setdefault("NFLPICKER_BUNDLE", str(Path(sys._MEIPASS)))

    # Before anything that might write to stdout, including uvicorn's logging
    # setup. See ensure_stdio: in a windowed build there is no stdout at all.
    ensure_stdio(Path(os.environ["NFLPICKER_DATA_DIR"]))

    if "--selftest" in sys.argv:
        return selftest()

    from nflpicker.desktop import run

    return run()


def leave(code: int) -> None:
    """Exit without waiting on background fetches. See nflpicker.desktop.leave,
    which the CLI's serve and desktop commands use for the same reason.

    The import is guarded because this is the last thing the process does. A
    packaged build that failed early enough to never import that module is
    exactly the case where it might also fail to import it now -- and an
    exception raised here would replace the exit code that says what went
    wrong with a traceback about the exit itself.
    """
    try:
        from nflpicker.desktop import leave as _leave
    except Exception:  # noqa: BLE001
        os._exit(code)
        return
    _leave(code)


if __name__ == "__main__":
    leave(main())
