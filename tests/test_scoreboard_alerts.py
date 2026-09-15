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


def _model(gid, home_prob):
    db.execute(
        "INSERT OR REPLACE INTO predictions(game_id, captured_at, model_version,"
        " margin_home, total_points, home_win_prob) VALUES(?,'t','v',0,42,?)",
        (gid, home_prob))


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
