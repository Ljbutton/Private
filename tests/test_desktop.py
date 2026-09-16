"""Desktop mode: the server-in-a-thread and the fallback when there is no GUI."""

import httpx
import pytest

from nflpicker.desktop import ServerThread, available, free_port


def test_a_free_port_is_loopback_only():
    """Binding anywhere but loopback would put an unauthenticated dashboard on
    the network; the app has no login."""
    port = free_port()
    assert 1024 < port < 65536
    assert ServerThread(port=port).url.startswith("http://127.0.0.1:")


def test_availability_is_a_boolean_not_an_exception():
    """Headless machines must get False rather than a traceback — importing
    pywebview succeeds even with no GUI backend, and only fails later."""
    assert isinstance(available(), bool)


@pytest.mark.usefixtures("temp_env")
def test_the_server_starts_and_stops_cleanly():
    server = ServerThread()
    url = server.start()
    try:
        assert httpx.get(f"{url}/api/state", timeout=10).status_code == 200
        assert httpx.get(f"{url}/", timeout=10).status_code == 200
    finally:
        server.stop()

    # After stopping, the port must no longer answer — a lingering server
    # would hold the port and the next launch would pick a different one.
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{url}/api/state", timeout=2)


def test_the_frozen_entry_point_picks_a_per_user_data_directory():
    import sys
    from pathlib import Path

    sys.path.insert(0, "scripts")
    from desktop_entry import default_data_dir

    path = default_data_dir()
    assert isinstance(path, Path)
    # Either name is correct: a fresh install gets the new one, an install
    # that predates the rename keeps the directory its data is already in.
    assert path.name in {"TheEdge", "NFLPicker"}
    assert path.is_absolute()


# ------------------------------------------------- the native window opening

def test_a_resolvable_backend_means_the_native_window_is_used(monkeypatch):
    """The regression this guards is the reason the app opened a browser tab on
    every platform for weeks.

    pywebview's package carries a module-level ``guilib = None`` that it only
    replaces with the real backend inside ``webview.start()``. So
    ``from webview import guilib`` binds None, ``None.initialize()`` raises
    AttributeError, and an ``except Exception`` swallowed it -- available()
    returned False always, and the app silently fell back to the browser.

    Patching the submodule in sys.modules is exactly the case the old code got
    wrong: the package attribute is still None, so anything reading the
    attribute sees None while the module itself is perfectly fine.
    """
    import sys
    import types

    from nflpicker import desktop

    # A display, so the headless guard below does not answer first: this test
    # is about resolving the backend, not about whether one could be shown.
    monkeypatch.setenv("DISPLAY", ":99")

    calls = []
    fake = types.ModuleType("webview.guilib")
    fake.initialize = lambda: calls.append("initialized")
    monkeypatch.setitem(sys.modules, "webview.guilib", fake)

    assert desktop.available() is True
    assert calls == ["initialized"]


def test_no_backend_falls_back_rather_than_crashing(monkeypatch):
    """A machine with no GUI toolkit must still get a usable app."""
    import sys
    import types

    from nflpicker import desktop

    monkeypatch.setenv("DISPLAY", ":99")
    fake = types.ModuleType("webview.guilib")

    def boom():
        raise RuntimeError("no GTK, no Qt")

    fake.initialize = boom
    monkeypatch.setitem(sys.modules, "webview.guilib", fake)

    assert desktop.available() is False


def test_a_linux_box_with_no_display_never_starts_a_toolkit(monkeypatch):
    """Resolving a backend *starts* one. Qt's initialize() builds a
    QApplication, and with no display that is a C-level abort rather than an
    exception -- nothing in Python can catch it and the process just dies. The
    cheap question has to come first.
    """
    import sys as _sys
    import types

    from nflpicker import desktop

    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    tripped = []
    fake = types.ModuleType("webview.guilib")
    fake.initialize = lambda: tripped.append("started a toolkit")
    monkeypatch.setitem(_sys.modules, "webview.guilib", fake)

    assert desktop.available() is False
    assert tripped == []          # and it never asked


def test_a_display_lets_the_check_proceed(monkeypatch):
    import sys as _sys
    import types

    from nflpicker import desktop

    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    fake = types.ModuleType("webview.guilib")
    fake.initialize = lambda: None
    monkeypatch.setitem(_sys.modules, "webview.guilib", fake)

    assert desktop.available() is True


# ------------------------------------------------- failures you can see

def test_startup_is_written_to_a_log_file(tmp_path, monkeypatch):
    """The packaged Windows build is windowed, so there is no console at all.

    Every print and traceback on the way to the window goes nowhere, which made
    a failure to start look exactly like double-clicking the icon and nothing
    happening -- with no record anywhere of why.
    """
    import logging

    from nflpicker import desktop

    monkeypatch.setattr(desktop, "log_path", lambda: tmp_path / "logs" / "desktop.log")
    path = desktop._start_logging()
    try:
        assert path is not None and path.exists()
        logging.getLogger("nflpicker.desktop").warning("something went wrong")
        assert "something went wrong" in path.read_text(encoding="utf-8")
    finally:
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.FileHandler):
                logging.getLogger().removeHandler(h)
                h.close()


def test_a_frozen_build_gets_longer_to_start(monkeypatch):
    """A onefile build unpacks a quarter of a gigabyte before Python runs, and
    an antivirus scans all of it. The source-tree timeout is far too short."""
    import importlib
    import sys as _sys

    from nflpicker import desktop

    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    reloaded = importlib.reload(desktop)
    try:
        assert reloaded.STARTUP_TIMEOUT > 60.0
    finally:
        monkeypatch.undo()
        importlib.reload(desktop)


def test_a_browser_that_will_not_open_is_reported_not_slept_through(monkeypatch):
    """This used to suppress the failure and then sleep for an hour at a time.

    With no console and no window that is a process running invisibly with
    nothing on screen -- indistinguishable from a crash, and only killable from
    Task Manager.
    """
    import webbrowser

    from nflpicker import desktop

    monkeypatch.setattr(webbrowser, "open", lambda _url: False)
    monkeypatch.setattr(desktop.time, "sleep", _never_sleep)

    seen = []
    monkeypatch.setattr(desktop, "_alert", lambda title, msg: seen.append(msg))

    class _Server:
        stopped = False

        def stop(self):
            type(self).stopped = True

    server = _Server()
    assert desktop._open_in_browser("http://127.0.0.1:9/", server) == 1
    assert _Server.stopped
    assert seen and "http://127.0.0.1:9/" in seen[0]


def test_a_server_that_will_not_start_says_so(monkeypatch):
    from nflpicker import desktop

    monkeypatch.setattr(desktop, "_start_logging", lambda: None)

    class _Server:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("server did not start within 240s")

        def stop(self):
            pass

    monkeypatch.setattr(desktop, "ServerThread", _Server)
    seen = []
    monkeypatch.setattr(desktop, "_alert", lambda title, msg: seen.append(msg))

    assert desktop.run() == 1
    assert seen and "did not start" in seen[0]


def _never_sleep(_seconds):
    raise AssertionError("slept instead of reporting the failure")


def test_a_window_that_never_appeared_is_not_a_clean_exit(monkeypatch):
    """webview.start() runs the platform event loop, so it returns when the
    window closes. Returning at once means nothing was ever on screen -- which
    is what a bundle missing its backend DLLs did, while exiting 0 and looking
    like a normal run."""
    import sys as _sys
    import types

    from nflpicker import desktop

    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **k: types.SimpleNamespace(destroy=lambda: None)
    fake.start = lambda **k: None          # returns immediately, raises nothing
    monkeypatch.setitem(_sys.modules, "webview", fake)

    monkeypatch.setattr(desktop, "available", lambda: True)
    monkeypatch.setattr(desktop, "_start_logging", lambda: None)

    class _Server:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return "http://127.0.0.1:9/"

        def stop(self):
            pass

    monkeypatch.setattr(desktop, "ServerThread", _Server)
    seen = []
    monkeypatch.setattr(desktop, "_alert", lambda title, msg: seen.append(msg))

    assert desktop.run() == 1
    assert seen and "without showing a window" in seen[0]


def test_a_window_the_user_actually_closed_is_a_clean_exit(monkeypatch):
    import sys as _sys
    import types

    from nflpicker import desktop

    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **k: types.SimpleNamespace(destroy=lambda: None)

    clock = iter([100.0, 100.0 + desktop.WINDOW_TOO_FAST + 30.0])
    fake.start = lambda **k: None
    monkeypatch.setitem(_sys.modules, "webview", fake)
    monkeypatch.setattr(desktop.time, "monotonic", lambda: next(clock))

    monkeypatch.setattr(desktop, "available", lambda: True)
    monkeypatch.setattr(desktop, "_start_logging", lambda: None)

    class _Server:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return "http://127.0.0.1:9/"

        def stop(self):
            pass

    monkeypatch.setattr(desktop, "ServerThread", _Server)
    monkeypatch.setattr(desktop, "_alert", lambda title, msg: pytest.fail(msg))

    assert desktop.run() == 0


# --------------------------------------------- the windowed build has no stdout

def test_the_server_starts_when_there_is_no_stdout(monkeypatch):
    """A `console=False` build sets sys.stdout to None, and uvicorn's default
    log formatter picks its colours with `sys.stdout.isatty()`.

    That raised inside `uvicorn.Config(...)`, reported as the opaque "Unable to
    configure formatter 'default'", before the server ever started -- so the
    app exited with no window. Every test missed it because capturing a
    subprocess's output *gives it* a stdout: running --selftest redirected,
    the only way to read it, was enough to hide the bug.

    Asserted against the formatter rather than uvicorn's LOGGING_CONFIG,
    because configure_logging() mutates that module-level dict in place -- by
    this point in a suite it may already carry use_colors and the failure
    could not be reproduced from it.
    """
    from uvicorn.logging import DefaultFormatter

    monkeypatch.setattr("sys.stdout", None)

    with pytest.raises(AttributeError, match="isatty"):
        DefaultFormatter(fmt="%(levelprefix)s %(message)s", use_colors=None)

    # What ServerThread now pins, so the server never asks stdout anything.
    assert DefaultFormatter(
        fmt="%(levelprefix)s %(message)s", use_colors=False).use_colors is False


def test_the_server_pins_use_colors_rather_than_asking_stdout():
    """The pin has to be in the call, not just in a passing test above it."""
    import inspect

    from nflpicker import desktop

    assert "use_colors=False" in inspect.getsource(desktop.ServerThread.start)


def test_a_missing_stdout_is_replaced_before_anything_uses_it(tmp_path, monkeypatch):
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location(
        "desktop_entry", "scripts/desktop_entry.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)

    monkeypatch.setattr(_sys, "stdout", None)
    monkeypatch.setattr(_sys, "stderr", None)
    entry.ensure_stdio(tmp_path)

    assert _sys.stdout is not None and _sys.stderr is not None
    print("captured instead of crashing")
    _sys.stdout.flush()
    assert "captured instead of crashing" in (
        tmp_path / "logs" / "console.log").read_text(encoding="utf-8")


def test_a_real_stdout_is_left_alone(tmp_path):
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location(
        "desktop_entry", "scripts/desktop_entry.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)

    before = _sys.stdout
    entry.ensure_stdio(tmp_path)
    assert _sys.stdout is before
    assert not (tmp_path / "logs").exists()


def _entry():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "desktop_entry", "scripts/desktop_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_process_leaves_without_waiting_for_background_fetches(monkeypatch):
    """Closing the window has to end the process, not merely the window.

    The startup refresh runs on ``asyncio.to_thread``, which means the default
    ThreadPoolExecutor, and concurrent.futures joins every worker of that pool
    at interpreter shutdown regardless of the daemon flag. So a normal return
    from main() hangs until the slowest outstanding HTTP request gives up --
    minutes, with retries and a backoff, on a bad network -- and the user is
    left with an app they closed still holding the port and the database.

    Neither marking threads daemon nor stopping the server can reach that,
    so the entry point exits hard instead.
    """
    entry = _entry()
    called: dict = {}
    monkeypatch.setattr(entry.os, "_exit", lambda code: called.setdefault("code", code))

    entry.leave(0)
    assert called["code"] == 0, "leave() must go through os._exit, not a normal return"


def test_leaving_closes_the_database_first(monkeypatch):
    """os._exit runs no atexit handlers, so WAL is checkpointed on the way out
    rather than left to the next launch to recover."""
    entry = _entry()
    order: list[str] = []
    monkeypatch.setattr(entry.os, "_exit", lambda code: order.append("exit"))

    from nflpicker import db

    monkeypatch.setattr(db, "close_all", lambda: order.append("close"))
    entry.leave(0)
    assert order == ["close", "exit"]


def test_leaving_survives_a_missing_stdout(monkeypatch):
    """A windowed build can have sys.stdout set to None; flushing it must not
    be the thing that stops the app from exiting."""
    entry = _entry()
    monkeypatch.setattr(entry.sys, "stdout", None)
    monkeypatch.setattr(entry.sys, "stderr", None)
    monkeypatch.setattr(entry.os, "_exit", lambda code: None)

    entry.leave(3)   # must not raise


def test_the_entry_point_uses_the_hard_exit(monkeypatch):
    """Guards the actual bug: `raise SystemExit(main())` returns normally and
    therefore hangs. The file has to call leave()."""
    source = open("scripts/desktop_entry.py", encoding="utf-8").read()
    tail = source.split('if __name__ == "__main__":')[-1]
    assert "leave(main())" in tail
    assert "SystemExit" not in tail
