"""The scoreboard on its own clock.

Scores and the game clock are the only thing in this app that is wrong within
seconds of changing. Everything else -- ratings, projections, the season
simulation -- is settled until a game *ends*. These pin that separation: the
cheap poll must stay cheap, must not be allowed to stampede the upstream feed,
and the expensive pass must not be dragged along behind it.
"""

import pytest
from fastapi.testclient import TestClient

from nflpicker import db


@pytest.fixture()
def client(temp_env):
    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        yield c


def _game(week=1, season=2026, status="in_progress", gid="live-1"):
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, season_type, kickoff,"
        " home, away, status, home_score, away_score, updated_at) "
        "VALUES(?,?,?,'REG','2026-09-10T17:00:00Z','KC','BUF',?,?,?,"
        "'2026-09-10T18:00:00Z')",
        (gid, season, week, status, 14, 10))
    return gid


def test_the_live_payload_carries_the_clock_not_just_the_score(client):
    """"In progress" is the one thing a reader can already see from the scores
    being there. The quarter and the clock are what say whether the score
    still means anything."""
    gid = _game()
    db.execute(
        "INSERT OR REPLACE INTO live_state(game_id, updated_at, period, clock,"
        " down, distance, red_zone, win_prob_home) "
        "VALUES(?, '2026-09-10T18:00:00Z', 3, '11:49', 1, 10, 0, 0.62)", (gid,))

    body = client.get("/api/live?season=2026&week=1").json()
    row = next(g for g in body["games"] if g["game_id"] == gid)
    assert row["home_score"] == 14 and row["away_score"] == 10
    assert row["live"]["period"] == 3
    assert row["live"]["clock"] == "11:49"
    # The probability is the poll's own, not the one seeded above: each poll
    # recomputes it from the score and the clock against the pregame
    # projection, which is the whole reason it is worth polling.
    assert 0.0 < row["live"]["home_win_prob"] < 1.0


def test_a_finished_game_carries_no_live_block(client):
    """The block is what a live card is drawn from, and a final has no clock.
    Leaving a stale one attached would leave "Q3 · 11:49" under a final
    score."""
    gid = _game(status="final")
    db.execute(
        "INSERT OR REPLACE INTO live_state(game_id, updated_at, period, clock,"
        " red_zone) VALUES(?, '2026-09-10T18:00:00Z', 3, '11:49', 0)", (gid,))

    body = client.get("/api/live?season=2026&week=1").json()
    row = next(g for g in body["games"] if g["game_id"] == gid)
    assert row["live"] is None
    # The kickoff still travels: a final's stamp is a date, and the page
    # redraws that stamp from this payload.
    assert row["kickoff"]


def test_several_windows_cost_one_request(client, monkeypatch):
    """The page polls every fifteen seconds and a person may have two windows
    open. The limit lives here rather than in the browser, because the browser
    is the thing there can be several of."""
    from nflpicker.api import create_app  # noqa: F401  (app already built)

    _game()
    calls: list[int] = []

    # Stand in for the fetch, so this measures the gate and not the feed.
    import nflpicker.pipeline as pipeline_module

    original = pipeline_module.Pipeline.upsert_games

    def counted(self, games):
        calls.append(1)
        return original(self, games)

    monkeypatch.setattr(pipeline_module.Pipeline, "upsert_games", counted)

    first = client.get("/api/live?season=2026&week=1").json()
    second = client.get("/api/live?season=2026&week=1").json()
    third = client.get("/api/live?season=2026&week=1").json()

    assert first.get("fetched") is True, "the first poll goes out"
    assert second.get("fetched") is False, "the second is inside the window"
    assert third.get("fetched") is False
    assert len(calls) == 1, "one upstream fetch between three pollers"
    # And the games still come back, because a rate-limited poll is not an
    # empty one -- it reads what the last fetch stored.
    assert second["games"] and third["games"]


def test_the_refresh_button_does_not_force_the_expensive_pass(pipeline, temp_env):
    """What "the refresh button is slow, or does nothing" was made of.

    The button asks for every stage regardless of its interval, which is
    right: pressing it means "go and fetch now". It used to force the
    analytical pass along with them -- Elo over every game ever played, the
    model over the whole history and twenty thousand simulated seasons -- even
    when every fetch had come back with rows already in the database. Two
    seconds to write numbers identical to the stored ones, and a page that
    looked exactly as it had.

    The fingerprint decides instead, and it errs towards recomputing. Naming
    the pass still forces it, which is what the CLI relies on.
    """
    import asyncio

    from nflpicker.scheduler import Scheduler

    forced: list[bool] = []
    pipeline.refresh_recompute = lambda result, force=False: forced.append(force)

    # Through the scheduler, because that is the path the button takes and the
    # place the decision is now made. Calling the pipeline directly with the
    # same stage list would prove nothing: the list contains "recompute", and
    # that is exactly the coincidence this guards against.
    scheduler = Scheduler(pipeline)
    asyncio.run(scheduler.refresh_now(None, full=True))
    assert forced == [False], "the button leaves the decision to the mark"

    pipeline.refresh(["recompute"])
    assert forced == [False, True], "naming the pass still forces it"
