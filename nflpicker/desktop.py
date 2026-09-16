"""Run the dashboard as a native desktop window instead of a browser tab.

The app is a local web app, so "native" here means the same server rendered
inside an OS window rather than a browser: pywebview wraps the platform's own
engine — WebView2 on Windows, WebKit on macOS, WebKitGTK on Linux — so nothing
ships a second browser and the window behaves like an application. No Chrome
tab, no address bar, no localhost URL to remember.

The server still runs, on a port chosen at startup and bound to loopback only.
That matters: the app has no authentication, and binding anywhere else would
put an unauthenticated dashboard on the network.

If no webview runtime is available, this degrades to opening the default
browser rather than failing — a missing GUI library should not stop the app
from being usable.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from .config import get_config

log = logging.getLogger("nflpicker.desktop")

WINDOW_TITLE = "NFL Picker"

# A onefile build unpacks its whole payload -- scipy, scikit-learn, pandas,
# pyarrow -- to a temp directory before a line of Python runs, and on Windows
# an antivirus scans every file as it lands. Importing them afterwards out of
# that cold directory is slow again. 60s is a reasonable wait for a developer
# running from source and much too short for a packaged first launch, where
# overrunning it meant the app exited without ever showing anything.
STARTUP_TIMEOUT = 240.0 if getattr(sys, "frozen", False) else 60.0

# Below this, the window loop cannot have shown anything a person could use.
WINDOW_TOO_FAST = 2.0


def log_path() -> Path:
    return Path(get_config().data_dir) / "logs" / "desktop.log"


def _start_logging() -> Path | None:
    """Send startup to a file, because a packaged build has nowhere else.

    The Windows executable is built windowed (`console=False`) so that no
    terminal sits behind the app. The cost is that every `print` and traceback
    on the way to the window goes nowhere at all: a failure to start looked
    exactly like double-clicking the icon and nothing happening, with no record
    anywhere of what went wrong.
    """
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        return path
    except Exception:                                     # noqa: BLE001
        return None


def _alert(title: str, message: str) -> None:
    """Say something the user can actually see, with no console and no window.

    Best effort by design -- it must never be the reason a start-up failure
    turns into a crash -- but on Windows it is the only channel there is.
    """
    with contextlib.suppress(Exception):
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
            return
        if sys.platform == "darwin":
            import subprocess

            subprocess.run(
                ["osascript", "-e",
                 f'display dialog {message!r} with title {title!r} buttons {{"OK"}}'],
                check=False, timeout=30,
            )
            return
    print(f"{title}: {message}", flush=True)


def _backend_name() -> str:
    """Which renderer pywebview settled on, for the log."""
    with contextlib.suppress(Exception):
        from importlib import import_module

        return str(getattr(import_module("webview.guilib"), "renderer", "unknown"))
    return "unknown"


def free_port() -> int:
    """An unused loopback port. Bound to 127.0.0.1 only, never 0.0.0.0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ServerThread:
    """Runs uvicorn in the background so the GUI owns the main thread.

    Most GUI toolkits require the main thread, so the server is the one that
    moves rather than the window.
    """

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        self.host = host
        self.port = port or free_port()
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        import uvicorn

        from .api import create_app

        config = uvicorn.Config(
            create_app(), host=self.host, port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        self._wait_until_ready()
        return self.url

    def _wait_until_ready(self, timeout: float = STARTUP_TIMEOUT) -> None:
        """Block until the server answers, so the window never opens on an error."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server is not None and getattr(self._server, "started", False):
                with contextlib.suppress(Exception):
                    httpx.get(f"{self.url}/api/state", timeout=3.0)
                    return
            time.sleep(0.2)
        raise RuntimeError(f"server did not start within {timeout:.0f}s")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)


def available() -> bool:
    """Is a usable webview *renderer* present?

    Importing pywebview is not the test: the package imports cleanly on a
    machine with no GUI toolkit at all, and only fails later inside
    ``webview.start()``. So this asks the library to resolve a backend and
    treats a failure as "not available".

    The module is fetched through ``import_module`` rather than with
    ``from webview import guilib``, and that detail is the whole function.
    pywebview's package sets a module-level ``guilib = None`` which it only
    replaces with the real backend inside ``webview.start()`` — so the plain
    ``from`` import binds **None**, ``None.initialize()`` raises AttributeError,
    the except swallowed it, and this returned False on every platform, every
    launch. The native window was never opening for anyone; the app silently
    opened a browser tab instead, which is the one thing packaging it was meant
    to avoid. ``import_module`` resolves the submodule itself and is unaffected
    by the package attribute shadowing it.
    """
    # On Linux, resolving a backend *starts a toolkit*: Qt's initialize() builds
    # a QApplication, and with no display that is a C-level abort, not an
    # exception -- the `except` below cannot catch it and the process simply
    # dies. Ask the cheap question first. Windows and macOS always have a window
    # server when there is a user, so the check is Linux-only.
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False

    try:
        from importlib import import_module

        guilib = import_module("webview.guilib")
    except Exception:  # noqa: BLE001
        return False
    try:
        # Raises WebViewException when no backend can be imported. On macOS it
        # also calls setup_app(), which must happen on the main thread -- run()
        # is the only caller and holds it.
        guilib.initialize()
    except Exception:  # noqa: BLE001
        return False
    return True


def _open_in_browser(url: str, server: ServerThread) -> int:
    """Fallback when no native window is possible."""
    import webbrowser

    hint = (
        f"No webview runtime found, so the dashboard is at {url}\n"
        "  Windows: install the Microsoft Edge WebView2 runtime\n"
        "  Linux:   install PyGObject and WebKitGTK "
        "(python3-gi gir1.2-webkit2-4.1)\n"
        "  macOS:   no extra install needed"
    )
    log.warning("no webview runtime; falling back to the browser at %s", url)
    print(hint, flush=True)

    opened = False
    try:
        opened = bool(webbrowser.open(url))
    except Exception as exc:                              # noqa: BLE001
        log.warning("could not open a browser: %s", exc)

    # Opening the browser used to be best-effort and silent, and then this
    # loop slept for ever. With no console and no window that is a process
    # running invisibly with nothing on screen -- indistinguishable from the
    # app having failed to start, and only killable from Task Manager.
    if not opened:
        _alert(
            WINDOW_TITLE,
            "NFL Picker could not open a window, and could not open your "
            f"browser either.\n\nOpen this address yourself:\n{url}\n\n"
            "On Windows, installing the Microsoft Edge WebView2 runtime gives "
            "you the proper app window.\n\nClosing this message quits.",
        )
        server.stop()
        return 1
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def run(*, width: int = 1400, height: int = 950, debug: bool = False) -> int:
    """Start the server and open it in a native window."""
    get_config().ensure_dirs()
    logfile = _start_logging()
    log.info("starting NFL Picker (frozen=%s, timeout=%.0fs)",
             getattr(sys, "frozen", False), STARTUP_TIMEOUT)
    server = ServerThread()

    print("starting NFL Picker…", flush=True)
    try:
        url = server.start()
    except Exception as exc:  # noqa: BLE001
        log.exception("could not start the server")
        print(f"could not start the server: {exc}", flush=True)
        _alert(
            WINDOW_TITLE,
            f"NFL Picker could not start.\n\n{exc}\n\n"
            + (f"Details: {logfile}" if logfile else "No log file could be written."),
        )
        return 1

    if not available():
        return _open_in_browser(url, server)

    import webview

    log.info("opening a native window via %s", _backend_name())
    window = webview.create_window(
        WINDOW_TITLE, url, width=width, height=height,
        min_size=(900, 640), confirm_close=False,
    )
    started = time.monotonic()
    try:
        webview.start(debug=debug)
    except Exception as exc:  # noqa: BLE001
        # A backend can resolve and still fail to open a window (no display,
        # missing runtime). The app must stay usable rather than exit.
        log.exception("native window failed")
        print(f"Could not open a native window ({exc}).", flush=True)
        return _open_in_browser(url, server)
    finally:
        # Closing the window must take the server with it, or the process
        # lingers holding a port.
        server.stop()
        with contextlib.suppress(Exception):
            window.destroy()

    # A backend can also fail *without raising*: webview.start() runs the
    # platform's event loop, so it returns when the window closes. Returning
    # immediately means no window was ever on screen -- which is what a
    # bundle missing its backend DLLs did, and it exited 0 looking like a
    # clean run. Nobody closes a window they asked for in under two seconds.
    elapsed = time.monotonic() - started
    if elapsed < WINDOW_TOO_FAST:
        log.error("the window loop returned after %.2fs; no window was shown", elapsed)
        _alert(
            WINDOW_TITLE,
            "NFL Picker opened and closed immediately without showing a "
            "window.\n\nThis usually means the webview backend could not "
            f"load.\n\nDetails: {log_path()}",
        )
        return 1
    return 0
