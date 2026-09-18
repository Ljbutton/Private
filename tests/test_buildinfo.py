"""Which build is running.

There was no way to answer that from inside the app, and it is the first
question worth asking when something is reported broken. Without it a report
and a fix pass each other indefinitely: the window looks identical either way,
so "still broken" and "already fixed" are both unfalsifiable.
"""

from nflpicker import buildinfo


def test_a_checkout_says_so_and_names_the_commit():
    buildinfo.build_info.cache_clear()
    info = buildinfo.build_info()
    assert info["source"] in {"checkout", "release"}
    assert info["commit"] and info["commit"] != "unknown"


def test_a_stamped_build_is_preferred_over_git(monkeypatch, tmp_path):
    """The packaged app has no git to ask, and its stamp is the truth."""
    import sys
    import types

    stamp = types.ModuleType("nflpicker._build")
    stamp.COMMIT = "abcdef1234567890"
    stamp.BUILT_AT = "2026-09-18T12:00:00Z"
    monkeypatch.setitem(sys.modules, "nflpicker._build", stamp)
    buildinfo.build_info.cache_clear()

    info = buildinfo.build_info()
    assert info == {"commit": "abcdef1", "built_at": "2026-09-18T12:00:00Z",
                    "source": "release"}
    assert buildinfo.label() == "build abcdef1 · 2026-09-18"
    buildinfo.build_info.cache_clear()


def test_neither_source_says_unknown_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(buildinfo, "_stamped", lambda: None)
    monkeypatch.setattr(buildinfo, "_from_git", lambda: None)
    buildinfo.build_info.cache_clear()

    assert buildinfo.build_info()["commit"] == "unknown"
    assert buildinfo.label() == "build unknown"
    buildinfo.build_info.cache_clear()


def test_the_state_endpoint_carries_it(temp_env):
    from fastapi.testclient import TestClient

    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as client:
        body = client.get("/api/state").json()
        assert body["build"]["commit"]
        assert body["build_label"].startswith("build ")
