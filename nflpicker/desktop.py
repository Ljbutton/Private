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
import socket
import threading
import time
from typing import Any

import httpx

from .config import get_config

log = logging.getLogger("nflpicker.desktop")

WINDOW_TITLE = "NFL Picker"
STARTUP_TIMEOUT = 60.0


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

    print(
        f"No webview runtime found, so the dashboard is at {url}\n"
        "  Windows: install the Microsoft Edge WebView2 runtime\n"
        "  Linux:   install PyGObject and WebKitGTK "
        "(python3-gi gir1.2-webkit2-4.1)\n"
        "  macOS:   no extra install needed\n"
        "Press Ctrl+C to stop.",
        flush=True,
    )
    with contextlib.suppress(Exception):
        webbrowser.open(url)
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
    server = ServerThread()

    print("starting NFL Picker…", flush=True)
    try:
        url = server.start()
    except Exception as exc:  # noqa: BLE001
        print(f"could not start the server: {exc}", flush=True)
        return 1

    if not available():
        return _open_in_browser(url, server)

    import webview

    window = webview.create_window(
        WINDOW_TITLE, url, width=width, height=height,
        min_size=(900, 640), confirm_close=False,
    )
    try:
        webview.start(debug=debug)
    except Exception as exc:  # noqa: BLE001
        # A backend can resolve and still fail to open a window (no display,
        # missing runtime). The app must stay usable rather than exit.
        log.warning("native window failed: %s", exc)
        print(f"Could not open a native window ({exc}).", flush=True)
        return _open_in_browser(url, server)
    finally:
        # Closing the window must take the server with it, or the process
        # lingers holding a port.
        server.stop()
        with contextlib.suppress(Exception):
            window.destroy()
    return 0
