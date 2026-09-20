"""Whether a newer build exists.

An app people pay for has to say when it is out of date -- the model changes
during the season and that is most of what a subscription buys. The thing to
get right is the silence: a check that cannot reach the internet, or a build
that does not know its own commit, must not claim anything.
"""

import httpx

from nflpicker import updates


class _Reply:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _release(monkeypatch, commit, published="2026-09-18T12:00:00Z"):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Reply(
        {"target_commitish": commit, "published_at": published}))


def _running(monkeypatch, commit, source="release"):
    monkeypatch.setattr(updates.buildinfo, "build_info",
                        lambda: {"commit": commit, "source": source, "built_at": ""})


def test_a_newer_release_is_reported(monkeypatch):
    updates.reset_cache()
    _running(monkeypatch, "aaaaaaa")
    _release(monkeypatch, "bbbbbbbcccccc")

    out = updates.check(force=True)
    assert out["newer"] is True
    assert out["latest"] == "bbbbbbb"
    assert out["url"].startswith("https://github.com/")


def test_the_same_build_is_not_an_update(monkeypatch):
    updates.reset_cache()
    _running(monkeypatch, "aaaaaaa")
    _release(monkeypatch, "aaaaaaadddddd")

    out = updates.check(force=True)
    assert out["newer"] is False and out["reason"] == "up to date"


def test_being_offline_claims_nothing(monkeypatch):
    """And is not cached, so a laptop that was offline can find out later."""
    updates.reset_cache()
    _running(monkeypatch, "aaaaaaa")

    def boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", boom)
    out = updates.check(force=True)
    assert out["newer"] is False
    assert "could not reach" in out["reason"]
    assert updates._cache["result"] is None, "not cached, so it can retry"


def test_a_checkout_is_never_nagged(monkeypatch):
    updates.reset_cache()
    _running(monkeypatch, "aaaaaaa", source="checkout")

    def boom(*a, **k):
        raise AssertionError("should not have asked the feed")

    monkeypatch.setattr(httpx, "get", boom)
    assert updates.check(force=True)["newer"] is False


def test_an_unknown_build_claims_nothing(monkeypatch):
    """Telling someone they are out of date when you do not know is worse than
    saying nothing at all."""
    updates.reset_cache()
    _running(monkeypatch, "unknown")
    _release(monkeypatch, "bbbbbbb")

    out = updates.check(force=True)
    assert out["newer"] is False
    assert "does not say which commit" in out["reason"]


def test_the_window_can_say_which_version_it_is(temp_env):
    """A commit hash is not a version.

    "build 12bc6ee" answers whether this is the copy with the fix, which is a
    developer's question. It does not answer the one a customer asks, which is
    which version they are on -- so the number they can say out loud rides
    alongside it in the same payload.
    """
    from fastapi.testclient import TestClient

    from nflpicker import __version__
    from nflpicker.api import create_app

    with TestClient(create_app()) as client:
        state = client.get("/api/state").json()

    assert state["version"] == __version__
    assert state["version"].count(".") == 2, "a full semantic version"
    # The build stays, because the two answer different questions.
    assert "build_label" in state and "build" in state
