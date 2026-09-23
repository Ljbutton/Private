"""Picks and survivor answer for any week, not only the one being played.

Both boards are recorded when they are computed, and they were only ever
computed for the week the season was on. So moving the week selector to a week
already played, or one not yet reached, found no stored row and drew an empty
page -- which reads as the page being broken rather than as it having been
asked a question nobody wired it to answer. Every game already carries a
prediction whatever week it is in, so the answer existed and was simply not
being assembled.
"""

import pytest
from fastapi.testclient import TestClient

from nflpicker import db


@pytest.fixture()
def client(temp_env):
    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        yield c


PAIRS = [("KC", "DEN"), ("BUF", "NYJ"), ("PHI", "DAL"), ("SF", "SEA")]


def _season(season=2026, weeks=range(1, 8), played_through=3):
    """A season where some weeks are finished and the rest are ahead."""
    db.execute("DELETE FROM games")
    db.execute("DELETE FROM predictions")
    for wk in weeks:
        final = wk <= played_through
        for i, (home, away) in enumerate(PAIRS):
            gid = f"g{wk}-{i}"
            db.execute(
                "INSERT INTO games(game_id, season, week, season_type, kickoff,"
                " home, away, status, home_score, away_score, updated_at) "
                "VALUES(?,?,?,'REG',?,?,?,?,?,?,?)",
                (gid, season, wk, f"2026-10-{wk:02d}T17:00:00Z", home, away,
                 "final" if final else "scheduled",
                 24 if final else None, 17 if final else None,
                 "2026-10-01T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO predictions(game_id, captured_at, model_version,"
                " margin_home, total_points, home_win_prob) "
                "VALUES(?,?,'t',3.0,44.0,?)",
                (gid, "2026-10-01T00:00:00Z", 0.80 - i * 0.05),
            )


def _board(client, week):
    body = client.get(f"/api/picks?season=2026&week={week}").json()
    return body


@pytest.mark.parametrize("week", [1, 2, 3, 4, 5, 6, 7])
def test_every_week_has_a_board(client, week):
    """Played, current and still to come all answer."""
    _season()
    body = _board(client, week)
    picks = (body["pickem"] or {}).get("ev", {}).get("picks") or []
    assert len(picks) == len(PAIRS), f"week {week} produced no pick'em board"
    assert body["survivor"], f"week {week} produced no survivor plan"


def test_a_played_week_keeps_the_numbers_it_was_given(client):
    """Not a rerun with the results known.

    Predictions are written once and kept, so a finished week reads back the
    probabilities the board committed to while those games were still
    upcoming. A board that quietly re-derived them would score itself against
    its own hindsight.
    """
    _season()
    picks = _board(client, 1)["pickem"]["ev"]["picks"]
    by_game = {p["game_id"]: p for p in picks}
    assert by_game["g1-0"]["win_prob"] == pytest.approx(0.80)
    assert by_game["g1-3"]["win_prob"] == pytest.approx(0.65)


def test_survivor_plans_forward_from_the_week_asked_about(client):
    """Week 5's plan is the decision week 5 faced, not week 1's."""
    _season()
    early = _board(client, 1)["survivor"]
    late = _board(client, 5)["survivor"]
    assert early["week"] == 1 and late["week"] == 5
    assert early["horizon"] > late["horizon"], (
        "a later week has fewer weeks left to plan")


def test_a_stored_board_is_preferred_to_a_rebuilt_one(client):
    """What was actually recorded at the time wins.

    The rebuild is a fallback for weeks that have no row, not a replacement
    for the history -- a recorded board is what the app really said that week,
    including any inputs that have since changed.
    """
    _season()
    db.execute(
        "INSERT OR REPLACE INTO pick_history"
        "(contest, season, week, captured_at, payload) VALUES(?,?,?,?,?)",
        ("pickem", 2026, 2, "2026-10-01T00:00:00Z",
         '{"ev": {"picks": [{"game_id": "stored", "pick": "KC"}]}}'),
    )
    picks = _board(client, 2)["pickem"]["ev"]["picks"]
    assert [p["game_id"] for p in picks] == ["stored"]


def test_a_week_with_no_games_says_nothing_rather_than_inventing(client):
    _season(weeks=range(1, 4), played_through=3)
    body = _board(client, 12)
    assert not ((body["pickem"] or {}).get("ev", {}).get("picks") or [])
