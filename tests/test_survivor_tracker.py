"""The original survivor run against the one actually picked.

A survivor pool is one long bet whose result arrives in instalments. "Was the
optimiser right" cannot be answered a week at a time -- spending a strong team
early is either the mistake that ends you in November or the reason you were
still alive to make it -- so the only thing that settles it is which of the two
runs busts first. That is what these pin.
"""

import pytest
from fastapi.testclient import TestClient

from nflpicker import db
from nflpicker.picks.survivor import track


def _game(week, home, away, hs=None, as_=None, status="final"):
    return {"week": week, "home": home, "away": away,
            "home_score": hs, "away_score": as_, "status": status}


GAMES = [
    _game(1, "KC", "DEN", 27, 10),
    _game(2, "BUF", "NYJ", 14, 20),
    _game(3, "PHI", "DAL", 24, 24),
    _game(4, "SF", "SEA", 30, 13, status="scheduled"),
]


def test_the_plan_is_walked_against_what_actually_happened():
    out = track([{"week": 1, "team": "KC"}, {"week": 2, "team": "BUF"}], {}, GAMES)
    weeks = out["original"]["weeks"]
    assert [w["result"] for w in weeks] == ["won", "lost"]
    assert out["original"]["out_week"] == 2
    assert out["original"]["alive"] is False
    assert weeks[0]["score"] == "DEN 10-27 KC"


def test_a_tie_survives_and_says_so():
    """Most pools advance a tie and some do not. It is counted as survival and
    labelled, rather than quietly resolved either way: the pool's rules decide
    that, and the reader knows theirs."""
    out = track([{"week": 3, "team": "PHI"}], {}, GAMES)
    assert out["original"]["weeks"][0]["result"] == "tied"
    assert out["original"]["alive"] is True


def test_a_game_that_has_not_finished_is_not_a_result():
    out = track([{"week": 4, "team": "SF"}], {}, GAMES)
    assert out["original"]["weeks"][0]["result"] == "pending"
    assert out["original"]["weeks_survived"] == 0


def test_the_verdict_names_whichever_run_went_out_first():
    out = track(
        [{"week": 1, "team": "DEN"}],            # lost in week 1
        {"KC": 1, "NYJ": 2},                     # won, then won
        GAMES,
    )
    assert out["original"]["out_week"] == 1
    assert out["mine"]["alive"] is True
    assert "still alive" in out["verdict"]
    assert "week 1" in out["verdict"]


def test_going_out_first_yourself_is_said_plainly():
    out = track([{"week": 1, "team": "KC"}], {"DEN": 1}, GAMES)
    assert "You went out in week 1" in out["verdict"]


def test_nothing_saved_yet_explains_itself():
    assert "No original run saved yet" in track([], {}, GAMES)["verdict"]
    assert "Nothing picked yet" in track(
        [{"week": 1, "team": "KC"}], {}, GAMES)["verdict"]


# ------------------------------------- what the plan would do if it had lived
#
# These go through the API rather than `track`, because the counterfactual is
# planned from the schedule and the predictions -- it is not something the
# walk over two finished runs can produce on its own.


@pytest.fixture()
def client(temp_env):
    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        yield c


def _season(weeks=6, played_through=2):
    """Four games a week, the home side winning every finished one."""
    db.execute("DELETE FROM games")
    db.execute("DELETE FROM predictions")
    pairs = [("KC", "DEN"), ("BUF", "NYJ"), ("PHI", "DAL"), ("SF", "SEA")]
    for week in range(1, weeks + 1):
        final = week <= played_through
        for i, (home, away) in enumerate(pairs):
            gid = f"g{week}-{i}"
            db.execute(
                "INSERT INTO games(game_id, season, week, season_type, kickoff,"
                " home, away, status, home_score, away_score, updated_at) "
                "VALUES(?,?,?,'REG',?,?,?,?,?,?,?)",
                (gid, 2026, week, f"2026-10-{week:02d}T17:00:00Z", home, away,
                 "final" if final else "scheduled",
                 27 if final else None, 10 if final else None,
                 "2026-10-01T00:00:00Z"))
            db.execute(
                "INSERT INTO predictions(game_id, captured_at, model_version,"
                " margin_home, total_points, home_win_prob) "
                "VALUES(?,?,'t',3.0,44.0,?)",
                (gid, "2026-10-01T00:00:00Z", 0.80 - i * 0.05))


def test_a_busted_plan_offers_what_it_would_do_from_here(client):
    """A dead plan stops being a rival and becomes a gravestone.

    Its column is weeks, a cross partway down, and nothing after it -- while
    the question it exists to answer, whether the optimiser beats your own
    picks, has the rest of the season left to run. So it also carries what it
    would take from here, planned around the teams you have actually spent:
    those are gone whatever an imaginary run would prefer.
    """
    from nflpicker.picks.survivor import ORIGINAL_KEY, USED_WEEKS_KEY

    _season()
    # The plan took the away side in week 2 and the away side always loses.
    db.set_meta(f"{ORIGINAL_KEY}:2026", {
        "path": [{"week": 1, "team": "KC"}, {"week": 2, "team": "DEN"}],
        "saved_at": "2026-09-01T00:00:00Z", "from_week": 1})
    db.set_meta(USED_WEEKS_KEY, {"KC": 1, "BUF": 2})

    body = client.get("/api/survivor/tracker?season=2026").json()
    assert body["original"]["out_week"] == 2, "the plan went out"

    cont = body["original"].get("continuation")
    assert cont and cont["path"], "a busted plan still says what it would do"
    assert not ({"KC", "BUF"} & {step["team"] for step in cont["path"]}), (
        "a team the picks have spent is gone for the counterfactual too")


def test_a_living_plan_needs_no_counterfactual(client):
    """Nothing to imagine while it is still running."""
    from nflpicker.picks.survivor import ORIGINAL_KEY, USED_WEEKS_KEY

    _season()
    db.set_meta(f"{ORIGINAL_KEY}:2026", {
        "path": [{"week": 1, "team": "KC"}, {"week": 2, "team": "BUF"}],
        "saved_at": "2026-09-01T00:00:00Z", "from_week": 1})
    db.set_meta(USED_WEEKS_KEY, {"KC": 1})

    body = client.get("/api/survivor/tracker?season=2026").json()
    assert body["original"]["alive"] is True
    assert "continuation" not in body["original"]
