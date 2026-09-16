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
    assert path.name == "NFLPicker"
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
