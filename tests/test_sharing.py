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


def test_the_model_s_own_side_travels_with_the_pick(client, keyed):
    """So the dashboard can show agreement and disagreement without the server
    having to hold a model of its own."""
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    a_game()
    db.execute(
        "INSERT OR REPLACE INTO predictions"
        "(game_id, captured_at, model_version, margin_home, total_points,"
        " home_win_prob) VALUES('demo-1','2026-10-06T12:00:00Z','t',6.0,44.0,0.71)")

    client.post("/api/my-picks", json={"game_id": "demo-1", "selection": "KC"})
    rows = sharing.pending()
    assert len(rows) == 1
    assert rows[0]["model_side"] == "KC", "the home side, at 71%"
    assert sharing._payload(rows)["picks"][0]["model_side"] == "KC"


def test_the_model_s_side_is_the_away_team_when_it_says_so(client, keyed):
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    a_game()
    db.execute(
        "INSERT OR REPLACE INTO predictions"
        "(game_id, captured_at, model_version, margin_home, total_points,"
        " home_win_prob) VALUES('demo-1','2026-10-06T12:00:00Z','t',-6.0,44.0,0.29)")

    client.post("/api/my-picks", json={"game_id": "demo-1", "selection": "KC"})
    assert sharing.pending()[0]["model_side"] == "DEN"


def test_no_prediction_means_no_claim_about_one(client, keyed):
    """A blank is honest; inventing the market's side and calling it the
    model's would not be."""
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    a_game()
    client.post("/api/my-picks", json={"game_id": "demo-1", "selection": "KC"})
    assert sharing.pending()[0]["model_side"] is None


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
    # On by default and not yet allowed to send: the page needs both facts to
    # know it has to put the notice up.
    assert body["enabled"] is True and body["needs_notice"] is True
    assert body["chosen"] is False
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


# --------------------------------------------------- default on, with notice
#
# Sharing is on for anybody who has not made a choice. The whole of what makes
# that defensible is the notice: on until told is one thing, on quietly is
# another, and these are the tests that keep them apart.

def test_a_new_install_is_on_but_silent_until_the_notice_is_answered(keyed):
    """Both halves. It is on -- and it sends nothing."""
    assert sharing.chosen() is False, "nobody has chosen anything yet"
    assert sharing.enabled() is True, "on by default"
    assert sharing.needs_notice() is True
    assert sharing.may_send() is False, "and so, nothing goes"
    assert sharing.enqueue("winner", game_id="demo-1", season=2026, week=5,
                           side="KC") is False
    assert sharing.pending() == []


def test_keep_sharing_leaves_it_on_and_lets_picks_through(client, keyed):
    """The primary button on the notice, and what it has to do."""
    body = client.post("/api/sharing",
                       json={"enabled": True, "notice_seen": True}).json()
    assert body["enabled"] is True and body["needs_notice"] is False
    assert sharing.may_send() is True
    assert sharing.enqueue("winner", game_id="demo-1", season=2026, week=5,
                           side="KC") is True


def test_turn_off_sends_nothing_ever(client, keyed):
    """The other button. Equally easy to press, and it has to mean it."""
    body = client.post("/api/sharing",
                       json={"enabled": False, "notice_seen": True}).json()
    assert body["enabled"] is False
    assert body["needs_notice"] is False, "they answered; do not ask again"
    assert sharing.may_send() is False

    a_game()
    client.post("/api/my-picks", json={"game_id": "demo-1", "selection": "KC"})
    assert sharing.pending() == [], "not even queued"


def test_somebody_who_turned_it_off_stays_off_across_the_update(keyed):
    """The upgrade path, and the one that would be a betrayal to get wrong.

    A user who switched this off before the default changed must not be opted
    back in by a later build deciding the default is now on. Their choice is
    written down, and a default only ever applies to somebody who has not made
    one.
    """
    sharing.set_enabled(False)
    assert sharing.chosen() is True

    # The update lands: the default flips, and the notice version moves on.
    assert sharing.DEFAULT_ON is True
    assert sharing.enabled() is False, "their choice outranks the new default"
    assert sharing.may_send() is False


def test_bumping_the_notice_version_asks_again(keyed, monkeypatch):
    """A changed bargain is not a silent one. If the wording moves materially,
    the version moves with it and everybody sees it once more."""
    sharing.mark_notice_seen()
    assert sharing.needs_notice() is False

    monkeypatch.setattr(sharing, "NOTICE_VERSION", sharing.NOTICE_VERSION + 1)
    assert sharing.needs_notice() is True
    assert sharing.may_send() is False, "and nothing goes while it is pending"

    # Answering the new one settles it again.
    sharing.mark_notice_seen(sharing.NOTICE_VERSION)
    assert sharing.needs_notice() is False


def test_the_notice_is_only_shown_once_per_version(keyed):
    sharing.mark_notice_seen()
    assert sharing.needs_notice() is False
    # A second launch, a third, a reload: the answer is recorded, not asked for.
    assert sharing.state()["needs_notice"] is False
    assert sharing.state()["notice_seen"] == sharing.NOTICE_VERSION


# ------------------------------------------------ the key goes, and is checked
#
# The endpoint took anything at first: a picker id and some picks, no proof of
# anything. Checking a subscription means sending the thing that identifies
# one, so the key now travels with each batch -- which is a claim the notice
# had to be corrected for, and a thing worth a test.

def test_a_batch_carries_the_key_that_proves_the_subscription(keyed, monkeypatch):
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    sharing.enqueue("winner", game_id="demo-1", season=2026, week=5, side="KC")

    sent = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            sent.update({"url": url, "body": json})
            return FakeResponse()

    monkeypatch.setattr("httpx.Client", FakeClient)
    assert sharing.flush()["sent"] == 1
    assert sent["url"].endswith("/v1/picks")
    assert sent["body"]["license_key"] == "EDGE-TEST-KEY-0001"
    assert sent["body"]["picker"] == keyed
    assert sharing.pending() == [], "and the queue is emptied on success"


def test_a_refusal_drops_the_batch_rather_than_retrying_for_ever(keyed, monkeypatch):
    """A lapsed subscription will not start working because the app asked
    sixty more times. A rate limit is the other way round -- that one is
    temporary, so those rows stay and wait."""
    sharing.mark_notice_seen()
    sharing.set_enabled(True)

    def answer(status):
        class FakeResponse:
            status_code = status

            def raise_for_status(self):
                raise RuntimeError(f"HTTP {status}")

        class FakeClient:
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None):
                return FakeResponse()

        return FakeClient

    sharing.enqueue("winner", game_id="demo-1", season=2026, week=5, side="KC")
    monkeypatch.setattr("httpx.Client", answer(403))
    assert sharing.flush()["reason"] == "refused"
    assert sharing.pending() == [], "dropped, not queued against a day that is not coming"

    sharing.enqueue("winner", game_id="demo-2", season=2026, week=5, side="DEN")
    monkeypatch.setattr("httpx.Client", answer(429))
    assert sharing.flush()["reason"] == "unreachable"
    assert len(sharing.pending()) == 1, "a rate limit is worth waiting out"


# ------------------------------------------------------ reporting results
#
# ESPN answers this app and refuses the licence server, so the app passes on
# the scores it already has. The server decides what to believe; this side is
# only responsible for sending what it saw, once.

def _final(game_id="demo-1", home_score=27, away_score=20, status="final"):
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, season_type,"
        " kickoff, home, away, home_score, away_score, status, updated_at)"
        " VALUES(?,2026,5,'REG','2026-10-11T17:00:00Z','KC','DEN',?,?,?,"
        "'2026-10-12T00:00:00Z')",
        (game_id, home_score, away_score, status))
    return game_id


class _Posted:
    """A licence server that takes everything and remembers what it got."""

    def __init__(self, status=200):
        self.status, self.calls = status, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        self.calls.append((url, json))
        outer = self

        class R:
            status_code = outer.status

            def raise_for_status(self):
                if outer.status >= 400:
                    raise RuntimeError(f"http {outer.status}")

        return R()


def test_only_finished_games_are_reported(client, keyed):
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    _final("done-1")
    _final("playing", status="in_progress")
    _final("nil", home_score=None, away_score=None)

    ids = [r["game_id"] for r in sharing.unreported_finals()]
    assert ids == ["done-1"]


def test_results_go_once_and_are_not_sent_again(client, keyed, monkeypatch):
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    _final("done-1")
    posted = _Posted()
    monkeypatch.setattr("httpx.Client", lambda *a, **k: posted)

    out = sharing.report_results()
    assert out == {"sent": 1, "reason": "ok"}
    url, body = posted.calls[0]
    assert url.endswith("/v1/results")
    assert body["picker"] == keyed
    assert body["license_key"] == "EDGE-TEST-KEY-0001"
    assert body["results"][0]["home_score"] == 27

    # The second wake has nothing new, so it makes no request at all.
    assert sharing.report_results() == {"sent": 0, "reason": "empty"}
    assert len(posted.calls) == 1


def test_a_report_that_did_not_go_is_tried_again(client, keyed, monkeypatch):
    # No attempt budget to run out of: the game is still finished and still
    # unreported, so it is still owed however long the network is down.
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    _final("done-1")

    class Dead(_Posted):
        def post(self, url, json=None):
            raise OSError("no network")

    monkeypatch.setattr("httpx.Client", lambda *a, **k: Dead())
    for _ in range(12):
        assert sharing.report_results() == {"sent": 0, "reason": "unreachable"}
    assert [r["game_id"] for r in sharing.unreported_finals()] == ["done-1"]

    ok = _Posted()
    monkeypatch.setattr("httpx.Client", lambda *a, **k: ok)
    assert sharing.report_results()["sent"] == 1


def test_a_refused_report_stops_asking(client, keyed, monkeypatch):
    sharing.mark_notice_seen()
    sharing.set_enabled(True)
    _final("done-1")
    monkeypatch.setattr("httpx.Client", lambda *a, **k: _Posted(status=403))
    assert sharing.report_results() == {"sent": 0, "reason": "refused"}
    assert sharing.unreported_finals() == []


def test_sharing_turned_off_reports_nothing(client, keyed, monkeypatch):
    # A game score is nobody's personal data, but somebody who turned sharing
    # off turned off talking to that server, and that is not ours to reread.
    sharing.mark_notice_seen()
    sharing.set_enabled(False)
    _final("done-1")
    posted = _Posted()
    monkeypatch.setattr("httpx.Client", lambda *a, **k: posted)
    assert sharing.report_results() == {"sent": 0, "reason": "off"}
    assert posted.calls == []


def test_nothing_is_reported_before_the_notice_is_answered(client, keyed, monkeypatch):
    sharing.set_enabled(True)                  # but the notice is unanswered
    _final("done-1")
    posted = _Posted()
    monkeypatch.setattr("httpx.Client", lambda *a, **k: posted)
    assert sharing.report_results()["reason"] == "off"
    assert posted.calls == []
