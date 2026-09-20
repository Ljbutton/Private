"""Subscription check and update notice.

The license server is faked at the httpx boundary; nothing here touches the
network.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def licensed(temp_env, monkeypatch):
    """A copy that requires a license, talking to a fake server."""
    monkeypatch.setenv("NFLPICKER_DEMO", "0")
    monkeypatch.setenv("NFLPICKER_LICENSE_SERVER", "https://license.example")
    monkeypatch.setenv("NFLPICKER_STORE_URL", "https://whop.com/the-edge")
    from nflpicker import licensing

    server = {"answer": {"valid": True, "reason": "ok", "status": "active"},
              "calls": [], "down": False}

    class FakeResponse:
        def __init__(self, data, code=200):
            self._data, self.status_code = data, code

        def json(self):
            return self._data

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            server["calls"].append((url, json))
            if server["down"]:
                raise OSError("no network")
            return FakeResponse(server["answer"])

        def get(self, url):
            server["calls"].append((url, None))
            if server["down"]:
                raise OSError("no network")
            return FakeResponse(server.get("latest", {}))

    monkeypatch.setattr(licensing, "_http", lambda *a, **k: FakeClient())
    return licensing, server


def test_source_checkout_and_demo_never_ask(temp_env, monkeypatch):
    from nflpicker import licensing

    monkeypatch.delenv("NFLPICKER_LICENSE_SERVER", raising=False)
    assert licensing.required() is False
    assert licensing.allowed() is True
    monkeypatch.setenv("NFLPICKER_LICENSE_SERVER", "https://license.example")
    monkeypatch.setenv("NFLPICKER_DEMO", "1")
    assert licensing.required() is False


def test_no_key_means_locked(licensed):
    licensing, _ = licensed
    assert licensing.required()
    assert licensing.allowed() is False
    assert licensing.status()["reason"] == "missing_key"


def test_good_key_unlocks_and_sends_a_hashed_machine_id(licensed):
    licensing, server = licensed
    out = licensing.activate("  ABC-123  ")
    assert out["valid"] is True
    assert out["key_hint"] == "…-123"
    url, body = server["calls"][0]
    assert url == "https://license.example/v1/validate"
    assert body["license_key"] == "ABC-123"
    assert len(body["machine_id"]) == 24
    assert licensing.allowed()


def test_bad_key_is_refused_with_the_servers_message(licensed):
    licensing, server = licensed
    server["answer"] = {"valid": False, "reason": "not_found", "message": "Not found."}
    out = licensing.activate("NOPE")
    assert out["valid"] is False
    assert out["message"] == "Not found."
    assert not licensing.allowed()


def test_first_activation_offline_is_not_accepted(licensed):
    licensing, server = licensed
    server["down"] = True
    out = licensing.activate("ABC-123")
    assert out["valid"] is False
    assert "internet" in out["message"]


def test_offline_within_grace_keeps_working(licensed, monkeypatch):
    licensing, server = licensed
    licensing.activate("ABC-123")
    server["down"] = True
    clock = [licensing._now() + 3 * 86400]
    monkeypatch.setattr(licensing, "_now", lambda: clock[0])
    st = licensing.recheck(force=True)
    assert st["valid"] is True
    assert st["offline"] is True
    assert 3.5 < st["grace_days_left"] <= 4.0
    clock[0] += 5 * 86400                       # eight days without an answer
    assert licensing.allowed() is False
    assert "more than a week" in licensing.status()["message"]


def test_cancelled_subscription_locks_at_once(licensed):
    licensing, server = licensed
    licensing.activate("ABC-123")
    server["answer"] = {"valid": False, "reason": "inactive", "message": "Ended."}
    st = licensing.recheck(force=True)
    assert st["valid"] is False
    assert st["message"] == "Ended."


def test_mistyped_new_key_does_not_replace_a_working_one(licensed):
    licensing, server = licensed
    licensing.activate("GOOD-KEY")
    server["answer"] = {"valid": False, "reason": "not_found", "message": "Nope"}
    licensing.activate("TYPO-KEY")
    assert licensing.saved_key() == "GOOD-KEY"
    assert licensing.allowed()


def test_api_is_locked_until_activation(licensed):
    from fastapi.testclient import TestClient

    from nflpicker.api import create_app

    licensing, _ = licensed
    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        r = c.get("/api/state")
        assert r.status_code == 402
        assert r.json()["license"]["required"] is True
        assert c.get("/").status_code == 200           # the page still loads
        assert c.get("/api/license").json()["valid"] is False
        r = c.post("/api/license/activate", json={"key": "ABC-123"})
        assert r.json()["valid"] is True
        assert c.get("/api/state").status_code == 200


def test_license_file_holds_no_more_than_it_needs(licensed):
    licensing, _ = licensed
    licensing.activate("ABC-123")
    data = json.loads(licensing._path().read_text())
    assert set(data) <= {"key", "valid", "last_ok", "last_attempt", "reason",
                         "message", "status", "offline_message"}


# ------------------------------------------------------------------ updates
# A licensed copy asks the license server, not GitHub, so the notice keeps
# working once the repository is private.

def _release(monkeypatch, commit="aaaaaaa1111"):
    from nflpicker import buildinfo

    monkeypatch.setattr(buildinfo, "build_info",
                        lambda: {"commit": commit[:7], "built_at": "2026-09-01T00:00:00Z",
                                 "source": "release"})


def test_licensed_copy_offers_a_licensed_download(licensed, monkeypatch):
    licensing, server = licensed
    from nflpicker import updates

    updates.reset_cache()
    _release(monkeypatch)
    licensing.activate("ABC-123")
    server["latest"] = {"commit": "bbbbbbb2222", "built_at": "2026-09-10T00:00:00Z",
                        "download_url": "https://license.example/v1/download"}
    monkeypatch.setattr(updates.sys, "platform", "win32")
    out = updates.check(force=True)
    assert out["newer"] is True
    assert out["latest"] == "bbbbbbb"
    assert out["url"].startswith("https://license.example/v1/download?key=ABC-123")
    assert "TheEdge-windows-setup.exe" in out["url"]
    assert any(url.endswith("/v1/latest") for url, _ in server["calls"])


def test_licensed_copy_same_build_is_not_offered(licensed, monkeypatch):
    _, server = licensed
    from nflpicker import updates

    updates.reset_cache()
    _release(monkeypatch)
    server["latest"] = {"commit": "aaaaaaa1111"}
    assert updates.check(force=True)["newer"] is False


def test_licensed_copy_without_a_key_links_the_store(licensed, monkeypatch):
    _, server = licensed
    from nflpicker import updates

    updates.reset_cache()
    _release(monkeypatch)
    server["latest"] = {"commit": "ccccccc", "download_url": "https://license.example/v1/download"}
    out = updates.check(force=True)
    assert out["newer"] is True
    assert out["url"] == "https://whop.com/the-edge"


def test_license_server_offline_is_quiet(licensed, monkeypatch):
    _, server = licensed
    from nflpicker import updates

    updates.reset_cache()
    _release(monkeypatch)
    server["down"] = True
    assert updates.check(force=True)["newer"] is False


# --------------------------------------------------------------- bug reports

def test_a_report_needs_a_server_and_a_key(temp_env, monkeypatch):
    from nflpicker import licensing

    """Every way this fails has to leave the report where it was.

    Someone reporting a bug is already having a bad time; losing what they
    wrote because the support server is unreachable would be a second one. So
    nothing raises -- each failure comes back as a reason the page shows next
    to the copy button, which always works.
    """
    monkeypatch.setattr(licensing, "server_url", lambda: "")
    out = licensing.send_report("something is wrong")
    assert out["sent"] is False and out["reason"] == "no_server"

    monkeypatch.setattr(licensing, "server_url", lambda: "https://example.invalid")
    monkeypatch.setattr(licensing, "saved_key", lambda: "")
    out = licensing.send_report("something is wrong")
    assert out["sent"] is False and out["reason"] == "no_key"

    monkeypatch.setattr(licensing, "saved_key", lambda: "ABC-123")
    assert licensing.send_report("   ")["reason"] == "empty"


def test_an_unreachable_server_is_a_reason_not_a_crash(temp_env, monkeypatch):
    import httpx

    from nflpicker import licensing

    monkeypatch.setattr(licensing, "server_url", lambda: "https://example.invalid")
    monkeypatch.setattr(licensing, "saved_key", lambda: "ABC-123")

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **k):
            raise httpx.ConnectError("no route")

    monkeypatch.setattr(licensing, "_http", lambda timeout=10.0: _Boom())
    out = licensing.send_report("the bracket is wrong")
    assert out["sent"] is False and out["reason"] == "unreachable"


def test_the_report_goes_to_the_licence_server_with_the_key(temp_env, monkeypatch):
    from nflpicker import licensing

    """The app does not know where reports end up, and cannot be made to say.

    It posts to the licence server it already talks to; the destination is a
    secret on that server, so it is not in a binary anyone can unpack.
    """
    seen = {}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None):
            seen["url"] = url
            seen["json"] = json

            class _R:
                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {"sent": True, "message": "Report sent. Thank you."}

            return _R()

    monkeypatch.setattr(licensing, "server_url", lambda: "https://edge.example")
    monkeypatch.setattr(licensing, "saved_key", lambda: "ABC-123")
    monkeypatch.setattr(licensing, "_http", lambda timeout=10.0: _Client())

    out = licensing.send_report("the bracket showed the wrong seed")
    assert out["sent"] is True
    assert seen["url"] == "https://edge.example/v1/report"
    assert seen["json"] == {"license_key": "ABC-123",
                            "report": "the bracket showed the wrong seed"}
