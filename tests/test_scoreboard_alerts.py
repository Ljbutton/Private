"""Your picks, the comparison table, and the alert feed."""

import pytest

from nflpicker import alerts, db, scoreboard
from nflpicker.config import reset_config


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("NFLPICKER_DATA_DIR", str(tmp_path))
    reset_config()
    db.close_all()
    db.connect()
    yield
    db.close_all()
    reset_config()


def _final(gid, week, home, away, home_score, away_score):
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, home, away, home_score,"
        " away_score, status, updated_at) VALUES(?,2026,?,?,?,?,?,'final','t')",
        (gid, week, home, away, home_score, away_score))


def _model(gid, home_prob, blind_margin=None):
    """One prediction row yields two pickers: the blind margin and the blended
    probability. By default they agree, which is the ordinary case; pass
    blind_margin to make them disagree."""
    if blind_margin is None:
        blind_margin = 0.0 if abs(home_prob - 0.5) < 1e-9 else (
            3.0 if home_prob > 0.5 else -3.0)
    db.execute(
        "INSERT OR REPLACE INTO predictions(game_id, captured_at, model_version,"
        " margin_home, total_points, home_win_prob) VALUES(?,'t','v',?,42,?)",
        (gid, blind_margin, home_prob))


def _book(gid, home_prob):
    db.execute(
        "INSERT OR REPLACE INTO consensus(game_id, captured_at, home_win_prob, n_books)"
        " VALUES(?,'t',?,6)", (gid, home_prob))


def _you(gid, week, selection):
    db.execute(
        "INSERT OR REPLACE INTO user_picks(season, week, game_id, contest, selection,"
        " updated_at) VALUES(2026,?,?,'straight',?,'t')", (week, gid, selection))


# ----------------------------------------------------------------- scoreboard

def test_each_picker_is_scored_on_the_games_it_had_a_view_on(store):
    _final("g1", 1, "KC", "BUF", 30, 20)      # KC won
    _model("g1", 0.7)                          # model: KC
    _book("g1", 0.4)                           # book: BUF
    _you("g1", 1, "KC")                        # you: KC

    rows = scoreboard.weekly(2026)
    assert len(rows) == 1
    tallies = rows[0].to_dict()["tallies"]

    assert tallies["you"]["correct"] == 1
    assert tallies["model"]["correct"] == 1
    assert tallies["book"]["wrong"] == 1
    # The prediction markets had no opinion, so they are not scored at all --
    # silence must not read as a wrong pick.
    assert tallies["market"]["n"] == 0


def test_a_tie_is_a_push_for_everyone(store):
    _final("g1", 1, "KC", "BUF", 20, 20)
    _model("g1", 0.7)
    _you("g1", 1, "BUF")

    tallies = scoreboard.weekly(2026)[0].to_dict()["tallies"]
    assert tallies["you"] == {"correct": 0, "wrong": 0, "push": 1, "n": 0, "rate": None}
    assert tallies["model"]["push"] == 1


def test_the_common_column_scores_only_games_everyone_picked(store):
    """The comparison that means something: a picker cannot look better by
    having an opinion only about the easy games."""
    _final("g1", 1, "KC", "BUF", 30, 20)
    _model("g1", 0.7)
    _book("g1", 0.7)
    _you("g1", 1, "KC")
    db.execute(
        "INSERT OR REPLACE INTO odds_snapshots(game_id, book, market, captured_at,"
        " home_price, away_price) VALUES('g1','kalshi','moneyline','t',-200,200)")

    _final("g2", 1, "SF", "SEA", 10, 30)      # SEA won
    _model("g2", 0.9)                          # model wrong; nobody else picked

    row = scoreboard.weekly(2026)[0].to_dict()
    assert row["tallies"]["model"]["n"] == 2          # model picked both
    assert row["common"]["model"]["n"] == 1           # only one game had everyone
    assert row["common"]["you"]["n"] == 1
    assert row["common"]["market"]["correct"] == 1


def test_an_exact_coin_flip_is_not_a_pick(store):
    _final("g1", 1, "KC", "BUF", 30, 20)
    _model("g1", 0.5)
    assert scoreboard.weekly(2026)[0].to_dict()["tallies"]["model"]["n"] == 0


def test_season_totals_add_the_weeks_up(store):
    _final("g1", 1, "KC", "BUF", 30, 20)
    _you("g1", 1, "KC")
    _final("g2", 2, "SF", "SEA", 10, 30)
    _you("g2", 2, "SF")

    report = scoreboard.report(2026)
    assert len(report["weeks"]) == 2
    assert report["totals"]["all"]["you"] == {
        "correct": 1, "wrong": 1, "push": 0, "n": 2, "rate": 0.5}


# --------------------------------------------------------------------- alerts

def test_the_same_event_is_only_raised_once(store):
    """Recompute runs every few minutes; anything keyed on current state would
    re-raise the same alert until kickoff."""
    alert = alerts.Alert(kind="starter", severity="urgent", title="KC: QB change",
                         game_id="g1", fingerprint="starter:g1:KC")

    assert alerts.record([alert]) == 1
    assert alerts.record([alert]) == 0
    assert len(alerts.recent()) == 1


def test_a_quarterback_ruled_out_raises_an_urgent_alert(store):
    raised = alerts.from_games([{
        "game_id": "g1", "home": "KC", "away": "BUF", "status": "scheduled",
        "availability": {"home": {"qb_change": True, "adjustment": -4.2}},
        "movement": {},
    }])
    assert [a.kind for a in raised] == ["starter"]
    assert raised[0].severity == "urgent"
    assert "KC" in raised[0].title


def test_crossing_a_key_number_is_flagged_and_landing_on_it_is_not(store):
    def move(open_, now):
        return alerts.from_games([{
            "game_id": "g1", "home": "KC", "away": "BUF", "status": "scheduled",
            "movement": {"spread_open": open_, "spread_now": now},
        }])

    assert [a.kind for a in move(-3.5, -2.5)] == ["key_number"]   # crossed 3
    assert [a.kind for a in move(-2.5, -3.5)] == ["key_number"]   # the other way
    assert move(-3.0, -2.5) == []        # opened on it, never crossed
    assert move(-2.5, -1.5) == []        # nowhere near one


def test_a_final_game_raises_nothing(store):
    assert alerts.from_games([{
        "game_id": "g1", "home": "KC", "away": "BUF", "status": "final",
        "availability": {"home": {"qb_change": True}},
        "movement": {"spread_open": -3.5, "spread_now": -2.5, "steam": True},
    }]) == []


def test_an_opener_disagreement_is_raised_but_only_a_large_one(store):
    def edge(value):
        return alerts.from_games([{
            "game_id": "g1", "home": "KC", "away": "BUF", "status": "scheduled",
            "movement": {"opener_edge": value},
        }])

    assert [a.kind for a in edge(2.5)] == ["edge"]
    assert [a.kind for a in edge(-3.0)] == ["edge"]
    assert edge(1.2) == []
    assert edge(None) == []


def test_marking_alerts_seen(store):
    alerts.record([alerts.Alert(kind="steam", severity="info", title="a",
                                fingerprint="f1")])
    assert len(alerts.recent(unseen_only=True)) == 1
    alerts.mark_seen()
    assert alerts.recent(unseen_only=True) == []


def test_by_team_scores_every_game_a_team_played(store):
    """Not games where the picker took that side -- games the team was in. The
    question is which teams are being read wrongly."""
    _final("g1", 1, "KC", "BUF", 30, 20)      # KC won
    _final("g2", 2, "SF", "KC", 10, 30)       # KC won again, away
    _model("g1", 0.9)                          # right
    _model("g2", 0.9)                          # picked SF: wrong
    _you("g1", 1, "KC")
    _you("g2", 2, "KC")

    rows = {r["team"]: r for r in scoreboard.by_team(2026)}
    assert rows["KC"]["games"] == 2
    assert rows["KC"]["tallies"]["model"]["rate"] == 0.5
    assert rows["KC"]["tallies"]["you"]["rate"] == 1.0
    # BUF played once and the model got that one right.
    assert rows["BUF"]["tallies"]["model"] == {
        "correct": 1, "wrong": 0, "push": 0, "n": 1, "rate": 1.0}


def test_by_team_is_sorted_by_the_model_best_first(store):
    _final("g1", 1, "KC", "BUF", 30, 20)
    _model("g1", 0.1)                          # badly wrong about KC and BUF
    _final("g2", 1, "SF", "SEA", 30, 20)
    _model("g2", 0.9)                          # right about SF and SEA

    order = [r["team"] for r in scoreboard.by_team(2026)]
    assert order.index("SF") < order.index("KC")


def test_teams_the_model_never_picked_sort_last(store):
    """A missing rate is not a score of zero, so it must not land at the bottom
    of the scale among the teams the model actually reads badly."""
    _final("g1", 1, "KC", "BUF", 30, 20)
    _model("g1", 0.1)                          # wrong: 0% on KC and BUF
    _final("g2", 1, "SF", "SEA", 30, 20)
    _you("g2", 1, "SF")                        # only you picked this one

    order = [r["team"] for r in scoreboard.by_team(2026)]
    assert order[-2:] == ["SEA", "SF"]         # no model opinion, so last


def test_alerts_can_be_fetched_for_one_game(store):
    alerts.record([
        alerts.Alert(kind="steam", severity="info", title="a", game_id="g1",
                     fingerprint="f1"),
        alerts.Alert(kind="steam", severity="info", title="b", game_id="g2",
                     fingerprint="f2"),
    ])
    rows = db.query("SELECT * FROM alerts WHERE game_id = ?", ("g1",))
    assert [r["title"] for r in rows] == ["a"]


# ------------------------------------------- the model inheriting book picks

def test_the_model_falls_back_to_the_book_when_it_has_no_pick(store):
    """Weeks from before the app existed have no prediction. Leaving the row
    blank loses the week; taking the book's pick keeps it."""
    _final("g1", 1, "KC", "BUF", 30, 20)
    _book("g1", 0.7)                           # book: KC. No model row at all.

    picks = scoreboard.picks_for(2026)
    assert picks["g1"][scoreboard.MODEL] == "KC"
    assert scoreboard.MODEL in picks["g1"][scoreboard.INHERITED]


def test_an_inherited_pick_is_counted_and_reported_as_borrowed(store):
    """Marked, not silent: on these games the model and the book agree by
    construction, so a season of them would show the two tied and mean nothing
    by it."""
    _final("g1", 1, "KC", "BUF", 30, 20)
    _book("g1", 0.7)
    _final("g2", 2, "SF", "SEA", 30, 20)
    _book("g2", 0.7)
    _model("g2", 0.7)                          # week 2 the model speaks for itself

    report = scoreboard.report(2026)
    assert report["totals"]["all"]["model"]["correct"] == 2
    assert report["totals"]["inherited"]["model"] == 1
    assert report["totals"]["inherited"]["book"] == 0
    assert report["weeks"][0]["inherited"]["model"] == 1
    assert report["weeks"][1]["inherited"]["model"] == 0


def test_the_model_keeps_its_own_pick_when_it_disagrees(store):
    _final("g1", 1, "KC", "BUF", 30, 20)
    _book("g1", 0.7)                           # book: KC
    _model("g1", 0.2)                          # model: BUF, and it is wrong

    picks = scoreboard.picks_for(2026)
    assert picks["g1"][scoreboard.MODEL] == "BUF"
    assert scoreboard.INHERITED not in picks["g1"]


def test_nothing_is_inherited_when_the_book_is_silent_too(store):
    _final("g1", 1, "KC", "BUF", 30, 20)
    _you("g1", 1, "KC")

    picks = scoreboard.picks_for(2026)
    assert scoreboard.MODEL not in picks["g1"]


def test_an_upcoming_game_never_borrows_the_book_s_pick(store):
    """The fallback is a failsafe for history, not a policy. Lending the model
    the book's pick for a game it has not weighed in on yet would be inventing
    an opinion rather than filling in a missing one."""
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, home, away, status,"
        " updated_at) VALUES('g9',2026,5,'KC','BUF','scheduled','t')")
    _book("g9", 0.7)

    picks = scoreboard.picks_for(2026)
    assert scoreboard.MODEL not in picks.get("g9", {})


# ------------------------------------------------- the blind model as a picker

def test_the_blind_model_is_scored_separately_from_the_blend(store):
    """The whole question the board exists to answer is whether blending with
    the line adds anything, and it cannot be asked if one column is both."""
    _final("g1", 1, "KC", "BUF", 30, 20)          # KC won
    # Blind leans BUF; the blend, pulled toward a line that likes KC, says KC.
    _model("g1", 0.7, blind_margin=-2.0)

    picks = scoreboard.picks_for(2026)
    assert picks["g1"][scoreboard.BLIND] == "BUF"
    assert picks["g1"][scoreboard.MODEL] == "KC"

    tallies = scoreboard.weekly(2026)[0].to_dict()["tallies"]
    assert tallies["blind"]["wrong"] == 1
    assert tallies["model"]["correct"] == 1


def test_a_blind_margin_of_zero_is_not_a_pick(store):
    _final("g1", 1, "KC", "BUF", 30, 20)
    _model("g1", 0.7, blind_margin=0.0)
    assert scoreboard.BLIND not in scoreboard.picks_for(2026)["g1"]


def test_the_blind_model_never_borrows_the_book_s_pick(store):
    """Only the blend inherits. Lending the blind model the book's pick would
    make three columns identical and destroy the one comparison they are for."""
    _final("g1", 1, "KC", "BUF", 30, 20)
    _book("g1", 0.7)

    picks = scoreboard.picks_for(2026)
    assert picks["g1"][scoreboard.MODEL] == "KC"     # blend inherits
    assert scoreboard.BLIND not in picks["g1"]       # blind does not


# ------------------------------------------------------------------ coverage

def test_a_source_with_no_picks_says_why_rather_than_showing_a_dash(store):
    """A bare dash is indistinguishable from a broken fetch. The sportsbook
    column is empty on an old week for a reason that can be stated."""
    _final("g1", 1, "KC", "BUF", 30, 20)
    _you("g1", 1, "KC")

    cover = scoreboard.report(2026)["coverage"]
    assert cover["you"] == {"picked": 1, "games": 1, "note": None}
    assert cover["book"]["picked"] == 0
    assert "cannot be backfilled" in cover["book"]["note"]
    assert cover["market"]["note"]
    assert cover["blind"]["note"]


def test_coverage_is_silent_when_a_source_has_picks(store):
    _final("g1", 1, "KC", "BUF", 30, 20)
    _book("g1", 0.7)
    cover = scoreboard.report(2026)["coverage"]
    assert cover["book"] == {"picked": 1, "games": 1, "note": None}


def test_coverage_says_nothing_at_all_before_any_game_is_final(store):
    """Nothing is missing yet, so nothing should be explained."""
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, home, away, status,"
        " updated_at) VALUES('g9',2026,5,'KC','BUF','scheduled','t')")
    assert all(v["note"] is None for v in scoreboard.report(2026)["coverage"].values())
