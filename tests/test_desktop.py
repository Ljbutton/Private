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
