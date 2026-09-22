"""Pick sharing: the gate, the pseudonym, and the way out.

This is the one feature in the app that sends a customer's own data somewhere
else, so most of what is worth testing is about what does *not* happen. The
promise made in the notice is a promise about the queue as much as about the
network: a pick made before the user has seen the notice leaves no trace at
all, rather than sitting in an outbox waiting for the moment it is allowed.
"""

import pytest
from fastapi.testclient import TestClient

from nflpicker import db, settings, sharing


@pytest.fixture(autouse=True)
def _clean_choice(monkeypatch):
    """Settings live in the process environment as well as in the file, so one
    test's choice would otherwise decide the next one's default."""
    monkeypatch.delenv(sharing.SETTING, raising=False)


@pytest.fixture()
def client(temp_env):
    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        yield c


@pytest.fixture()
def keyed(temp_env, monkeypatch):
    """An installation with a licence key, which is what gives it an id."""
    from nflpicker import licensing

    monkeypatch.setattr(licensing, "saved_key", lambda: "EDGE-TEST-KEY-0001")
    monkeypatch.setattr(licensing, "server_url", lambda: "https://edge.example")
    return sharing.picker_id()


def a_game(season=2026, week=5, game_id="demo-1", status="scheduled"):
    db.execute(
        "INSERT OR REPLACE INTO games"
        "(game_id, season, week, season_type, kickoff, home, away, status, updated_at) "
        "VALUES(?,?,?,'REG','2026-10-11T17:00:00Z','KC','DEN',?,'2026-10-06T00:00:00Z')",
        (game_id, season, week, status))
    return game_id


# ------------------------------------------------------------------- the id

def test_the_id_is_a_pseudonym_not_the_key(keyed):
    """Sixteen hex characters, stable, and nothing of the key in it."""
    assert len(keyed) == sharing.ID_CHARS
    assert all(c in "0123456789abcdef" for c in keyed)
    assert "EDGE-TEST-KEY-0001" not in keyed
    assert sharing.picker_id("EDGE-TEST-KEY-0001") == keyed, "stable for one key"
    assert sharing.picker_id("A-DIFFERENT-KEY") != keyed


def test_no_key_means_no_id_and_nothing_to_send(temp_env, monkeypatch):
    from nflpicker import licensing

    monkeypatch.setattr(licensing, "saved_key", lambda: "")
    assert sharing.picker_id() == ""
    assert sharing.may_send() is False


def test_the_default_name_is_the_one_the_spec_asks_for(keyed):
    assert sharing.default_name() == f"Picker #{keyed[:4]}"
    assert sharing.display_name() == sharing.default_name()
    sharing.set_display_name("  The Commissioner  ")
    assert sharing.display_name() == "The Commissioner"


# ------------------------------------------------------------ nothing leaks

def test_nothing_is_queued_before_the_notice_is_seen(keyed):
    """The heart of it. Sharing being on is not consent; the notice is."""
    sharing.set_enabled(True)
    assert sharing.needs_notice() is True
    assert sharing.enqueue("winner", game_id="demo-1", season=2026, week=5,
                           side="KC") is False
    assert sharing.pending() == []


def test_nothing_is_queued_while_sharing_is_off(keyed):
    sharing.mark_notice_seen()
    sharing.set_enabled(False)
    assert sharing.enqueue("winner", game_id="demo-1", season=2026, week=5,
                           side="KC") is False
    assert sharing.pending() == []


def test_a_pick_is_queued_once_both_are_true(keyed):
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    assert sharing.may_send() is True
    assert sharing.enqueue("winner", game_id="demo-1", season=2026, week=5,
                           side="KC", line=-3.5) is True
    rows = sharing.pending()
    assert len(rows) == 1
    assert rows[0]["side"] == "KC" and rows[0]["line"] == -3.5


def test_changing_a_pick_replaces_it_rather_than_sending_both(keyed):
    """What the server should hold is the pick as it stands, not a record of
    somebody changing their mind twice on a Thursday."""
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    for side in ("KC", "DEN", "KC"):
        sharing.enqueue("winner", game_id="demo-1", season=2026, week=5,
                        side=side)
    rows = sharing.pending()
    assert len(rows) == 1 and rows[0]["side"] == "KC"


def test_turning_it_off_drops_what_was_waiting(keyed):
    """Off has to mean off. A queue that survives the switch is a pile of
    picks the user has just said they did not want sent."""
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    sharing.enqueue("winner", game_id="demo-1", season=2026, week=5, side="KC")
    assert sharing.pending()
    sharing.set_enabled(False)
    assert sharing.pending() == []


def test_a_flush_with_the_switch_off_sends_nothing(keyed, monkeypatch):
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    sharing.enqueue("winner", game_id="demo-1", season=2026, week=5, side="KC")
    # Turn it off the hard way, leaving the row behind, so the flush itself is
    # what has to refuse rather than the queue being empty.
    settings.save({sharing.SETTING: "0"})
    calls = []
    monkeypatch.setattr("httpx.Client", lambda **kw: calls.append(kw))
    assert sharing.flush()["reason"] == "off"
    assert calls == []


# -------------------------------------------------------- through the app

def test_making_a_pick_shares_it_with_the_line_of_the_moment(client, keyed):
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    a_game()
    db.execute(
        "INSERT OR REPLACE INTO consensus"
        "(game_id, captured_at, spread_home, total_points, ml_home, home_win_prob, n_books) "
        "VALUES('demo-1','2026-10-06T12:00:00Z',-3.5,47.5,-180,0.64,4)")

    response = client.post("/api/my-picks",
                           json={"game_id": "demo-1", "selection": "KC"})
    assert response.status_code == 200
    rows = sharing.pending()
    assert len(rows) == 1
    assert rows[0]["kind"] == "winner" and rows[0]["side"] == "KC"
    assert rows[0]["line"] == -3.5, "the line as it was, not as it closes"
    assert rows[0]["total_line"] == 47.5
    assert rows[0]["book_prob"] == 0.64


def test_clearing_a_pick_takes_it_out_of_the_queue(client, keyed):
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    a_game()
    client.post("/api/my-picks", json={"game_id": "demo-1", "selection": "KC"})
    client.post("/api/my-picks", json={"game_id": "demo-1", "selection": ""})
    assert sharing.pending() == []


def test_a_pick_after_kickoff_is_never_shared(client, keyed):
    """Worth nothing to a leaderboard and everything to somebody gaming one."""
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    a_game(status="in_progress")
    client.post("/api/my-picks", json={"game_id": "demo-1", "selection": "KC"})
    assert sharing.pending() == []


def test_the_endpoint_reports_and_sets_the_state(client, keyed):
    body = client.get("/api/sharing").json()
    assert body["enabled"] is False and body["needs_notice"] is True
    assert body["picker_id"] == keyed

    body = client.post("/api/sharing",
                       json={"enabled": True, "notice_seen": True}).json()
    assert body["enabled"] is True and body["needs_notice"] is False


def test_deleting_sends_the_key_as_proof(client, keyed, monkeypatch):
    """The id is on the leaderboard, so it cannot be what authorises a delete.
    This is the only request in the feature that carries the licence key."""
    seen = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "deleted": 12}

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, json=None):
            seen.update({"method": method, "url": url, "json": json})
            return FakeResponse()

    monkeypatch.setattr("httpx.Client", FakeClient)
    out = client.post("/api/sharing/delete").json()
    assert out["ok"] is True and out["deleted"] == 12
    assert seen["method"] == "DELETE"
    assert seen["url"].endswith("/v1/picks")
    assert seen["json"] == {"picker": keyed,
                            "license_key": "EDGE-TEST-KEY-0001"}
