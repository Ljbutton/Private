"""The original survivor run against the one actually picked.

A survivor pool is one long bet whose result arrives in instalments. "Was the
optimiser right" cannot be answered a week at a time -- spending a strong team
early is either the mistake that ends you in November or the reason you were
still alive to make it -- so the only thing that settles it is which of the two
runs busts first. That is what these pin.
"""

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
