"""End-to-end: a full refresh in demo mode, then every API endpoint."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from nflpicker import db
from nflpicker.util import now_iso


@pytest.fixture()
def booted(pipeline):
    pipeline.refresh(["schedule", "odds", "prediction_markets", "news", "recompute"])
    return pipeline


def test_a_full_refresh_populates_every_table(booted):
    for table, minimum in (("games", 272), ("odds_snapshots", 100), ("consensus", 100),
                           ("predictions", 100), ("season_projections", 32), ("news", 1)):
        n = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
        assert n >= minimum, f"{table} only has {n} rows"


def test_every_stage_reports_success_in_demo_mode(pipeline):
    result = pipeline.refresh()
    assert result.to_dict()["ok"], result.stages
    for stage in ("schedule", "odds", "news", "recompute"):
        assert result.stages[stage]["ok"], stage


def test_odds_history_produces_a_movement_series(booted):
    """A first fetch already carries per-book timestamps, so the movement chart
    must have something to draw immediately rather than after days of polling."""
    from nflpicker.market import movement

    row = db.query_one(
        "SELECT game_id FROM games WHERE status != 'final' AND season = ? ORDER BY kickoff",
        (booted.season(),))
    summary = movement.summarise(row["game_id"], "spread")
    assert summary["n_points"] >= 2
    assert summary["open"] is not None and summary["current"] is not None


def test_consensus_never_mixes_in_future_quotes(booted):
    """Each consensus snapshot must be built only from quotes at or before its
    own timestamp, otherwise the 'history' is not history."""
    row = db.query_one(
        "SELECT game_id FROM games WHERE status != 'final' ORDER BY kickoff")
    gid = row["game_id"]
    snapshots = db.query(
        "SELECT captured_at, spread_home FROM consensus WHERE game_id = ? ORDER BY captured_at",
        (gid,))
    first = snapshots[0]
    earliest_quote = db.query_one(
        "SELECT MIN(captured_at) AS m FROM odds_snapshots WHERE game_id = ?", (gid,))
    assert first["captured_at"] >= earliest_quote["m"]


def test_recompute_is_idempotent(booted):
    before = db.query_one("SELECT COUNT(*) AS n FROM games")["n"]
    booted.recompute()
    booted.recompute()
    assert db.query_one("SELECT COUNT(*) AS n FROM games")["n"] == before


def test_predictions_are_stored_for_upcoming_games(booted):
    upcoming = {r["game_id"] for r in db.query(
        "SELECT game_id FROM games WHERE status != 'final' AND season = ?", (booted.season(),))}
    predicted = {r["game_id"] for r in db.query(
        "SELECT DISTINCT game_id FROM predictions WHERE model_version NOT LIKE '%backfill'")}
    assert upcoming and upcoming <= predicted


def test_backfill_is_labelled_so_in_sample_results_can_be_flagged(booted):
    filled = booted.backfill_predictions()
    assert filled > 0
    versions = {r["model_version"] for r in db.query(
        "SELECT DISTINCT model_version FROM predictions WHERE model_version LIKE '%backfill'")}
    assert versions


def test_played_games_get_the_model_s_own_number_not_the_book_s(booted):
    """A database first filled mid-season still shows a model pick per game.

    The board falls back to the sportsbook's number for any finished game with
    no prediction row, so leaving the already-played weeks empty put the book's
    opinion in the model's column for most of the season.
    """
    season = booted.season()
    played = {r["game_id"] for r in db.query(
        "SELECT game_id FROM games WHERE status = 'final' AND season = ?", (season,))}
    predicted = {r["game_id"] for r in db.query(
        "SELECT DISTINCT p.game_id FROM predictions p JOIN games g"
        " ON g.game_id = p.game_id WHERE g.season = ?", (season,))}
    assert played and played <= predicted


def test_a_played_game_keeps_the_number_the_model_committed_to(booted):
    """Recomputing must not rewrite history with today's view of an old game."""
    season = booted.season()
    row = db.query_one(
        "SELECT p.game_id, p.margin_home, p.model_version FROM predictions p"
        " JOIN games g ON g.game_id = p.game_id"
        " WHERE g.season = ? AND g.status = 'final'"
        " AND p.model_version NOT LIKE '%backfill' LIMIT 1", (season,))
    if row is None:  # every finished game predates this install
        return
    booted.recompute()
    after = db.query_one(
        "SELECT margin_home, model_version FROM predictions WHERE game_id = ?",
        (row["game_id"],))
    assert after["margin_home"] == row["margin_home"]
    assert after["model_version"] == row["model_version"]


def test_backfilled_totals_are_not_all_the_league_average(booted):
    """The power total is one offence against the other defence.

    Built without the efficiency and record inputs every team comes out
    league-average, and every game projects the identical total -- which is a
    silent failure, since the number still looks like a projection.
    """
    booted.backfill_predictions(overwrite=True)
    totals = [r["total_points"] for r in db.query(
        "SELECT total_points FROM predictions"
        " WHERE model_version LIKE '%backfill' AND total_points IS NOT NULL")]
    assert len(totals) > 10
    assert len(set(round(t, 3) for t in totals)) > 1


# ----------------------------------------------------------------------- api

@pytest.fixture()
def client(booted):
    from nflpicker.api import create_app

    app = create_app(start_scheduler=False, bootstrap=False)
    with TestClient(app) as c:
        yield c


def test_state_endpoint_describes_the_installation(client):
    body = client.get("/api/state").json()
    assert body["demo"] is True
    assert body["season"] and body["week"]
    assert len(body["weeks"]) == 18
    assert "model" in body and "sources" in body
    # The board draws a mark and a name for all thirty-two teams, and this is
    # where that reference reaches the browser.
    assert len(body["teams"]) == 32
    assert body["teams"]["KC"]["name"] == "Chiefs"
    # The greeting is rendered from this. It can legitimately be empty -- a
    # machine whose account is called `runner` has no name worth using -- so
    # what matters is that the shape is always there for the page to read.
    assert set(body["user"]) == {"name", "full", "source"}


def test_games_endpoint_returns_renderable_cards(client):
    body = client.get("/api/games").json()
    assert body["games"]
    card = body["games"][0]
    for key in ("game_id", "home", "away", "home_name", "prediction", "market", "movement"):
        assert key in card


def test_game_detail_includes_movement_books_and_history(client):
    games = client.get("/api/games").json()["games"]
    detail = client.get(f"/api/game/{games[0]['game_id']}").json()
    assert detail["movement"]["spread"]["points"]
    assert detail["latest_books"]
    assert detail["prediction_history"]


def test_unknown_game_is_a_404(client):
    assert client.get("/api/game/nope").status_code == 404


def test_teams_endpoint_ranks_all_32(client):
    body = client.get("/api/teams").json()
    assert len(body["teams"]) == 32
    assert body["teams"][0]["rank"] == 1
    playoff = sum(t["playoff_prob"] or 0 for t in body["teams"])
    assert abs(playoff - 14) < 0.1


def test_picks_endpoint_returns_all_three_contests(client):
    body = client.get("/api/picks").json()
    assert "edges" in (body["ats"] or {})
    assert "ev" in (body["pickem"] or {}) and "leverage" in (body["pickem"] or {})
    assert body["survivor"] is not None


def test_survivor_used_teams_round_trip(client):
    assert client.post("/api/survivor/used", json={"teams": ["KC", "sf"]}).status_code == 200
    assert client.get("/api/picks").json()["survivor_used"] == ["KC", "SF"]
    plan = client.get("/api/picks").json()["survivor"]
    assert all(e["team"] not in ("KC", "SF") for e in plan.get("path", []))


def test_unknown_survivor_team_is_rejected(client):
    assert client.post("/api/survivor/used", json={"teams": ["XXX"]}).status_code == 400


def test_news_and_performance_endpoints_respond(client):
    assert "items" in client.get("/api/news").json()
    assert "n_games" in client.get("/api/performance").json()


def test_the_dashboard_and_its_assets_are_served(client):
    assert client.get("/").status_code == 200
    for asset in ("app.js", "charts.js", "style.css"):
        res = client.get(f"/static/{asset}")
        assert res.status_code == 200 and len(res.content) > 500


def test_sportsbook_edges_are_never_priced_against_a_prediction_market(booted):
    """The cross-market panel bets *into* a prediction venue. A model edge
    quoted at that venue's price would merge two different claims and credit a
    sportsbook recommendation to a book that never offered it."""
    import json

    from nflpicker.venues import PREDICTION_MARKET_VENUES

    booted.recompute()
    row = db.query_one(
        "SELECT payload FROM pick_history WHERE contest = 'ats' "
        "ORDER BY captured_at DESC LIMIT 1"
    )
    edges = json.loads(row["payload"])["edges"] if row else []
    books = {(e.get("book") or "").lower() for e in edges}
    leaked = books & PREDICTION_MARKET_VENUES
    assert not leaked, f"sportsbook edges priced at a prediction market: {leaked}"


def test_prediction_market_view_is_comparison_only(booted):
    """It must carry no recommendation: no stake, no expected value, no rating."""
    import json

    booted.recompute()
    row = db.query_one(
        "SELECT payload FROM pick_history WHERE contest = 'prediction_markets' "
        "ORDER BY captured_at DESC LIMIT 1"
    )
    games = json.loads(row["payload"])["games"] if row else []
    assert games, "expected prediction-market comparisons in demo mode"
    for g in games:
        assert g["venues"], "a row exists only because some venue priced it"
        assert not {"expected_value", "kelly", "stake", "confidence"} & set(g)
        if g["gap"] is not None and g["book_prob"] is not None:
            # Each field is rounded to 4dp for the payload, so the identity
            # holds only to within that rounding, not exactly.
            assert abs((g["venue_prob"] - g["book_prob"]) - g["gap"]) < 5e-4


# ------------------------------------------------------- what decides the order

def test_the_ranking_is_not_a_standings_table(client):
    """The complaint this exists for: through September the order was very
    nearly the standings, because the rating is mostly Elo, Elo moves on
    results, and every other term in it fades in by games played."""
    from nflpicker.api import RANK_WEIGHTS

    assert "record" not in RANK_WEIGHTS
    assert "wins_actual" not in RANK_WEIGHTS
    # The measures that are about how good a team is carry most of the weight.
    forward = sum(RANK_WEIGHTS[k] for k in
                  ("exp_wins", "pythagorean", "sb_prob", "wins_p10"))
    assert forward > RANK_WEIGHTS["power"]
    assert abs(sum(RANK_WEIGHTS.values()) - 1.0) < 1e-9


def test_a_team_that_wins_ugly_does_not_outrank_one_that_loses_well():
    from nflpicker.api import ranking_scores

    ratings = {
        "AAA": {"power": 0.2, "pythagorean": 0.35},   # won, but outscored
        "BBB": {"power": 0.0, "pythagorean": 0.68},   # lost, but outscored
    }
    projections = {
        "AAA": {"exp_wins": 7.5, "wins_p10": 5, "sb_prob": 0.01},
        "BBB": {"exp_wins": 10.5, "wins_p10": 8, "sb_prob": 0.09},
    }
    scores = ranking_scores(ratings, projections)
    assert scores["BBB"] > scores["AAA"]


def test_the_order_is_the_same_every_time(client):
    """The Bears and the Ravens were swapping the top spot between renders.
    Equal scores have to land in the same order or the league looks unstable
    for a reason no reader can see."""
    from nflpicker.api import team_rank_order

    flat = {t: {"power": 1.0, "pythagorean": 0.5} for t in ("AAA", "BBB", "CCC")}
    projections = {t: {"exp_wins": 8.5, "wins_p10": 6, "sb_prob": 0.03}
                   for t in flat}
    first = team_rank_order(2026, flat, projections)
    for _ in range(5):
        assert team_rank_order(2026, flat, projections) == first
    assert first == ["AAA", "BBB", "CCC"]


def test_a_missing_measure_reads_as_average_rather_than_last():
    """Week one has no Pythagorean and no projections. A team missing a
    measure should sit where the others put it, not be dumped at the bottom."""
    from nflpicker.api import ranking_scores

    ratings = {"AAA": {"power": 3.0, "pythagorean": None},
               "BBB": {"power": -3.0, "pythagorean": None}}
    projections = {"AAA": {}, "BBB": {}}
    scores = ranking_scores(ratings, projections)
    assert scores["AAA"] > scores["BBB"]


# ------------------------------------------------- season odds against the market

def test_the_season_odds_are_pulled_toward_the_posted_totals():
    """Nobody prices week fourteen in September, so from a month out the
    simulation was running on ratings alone. Season win totals are posted all
    year, and they are the only view the market has on the whole run."""
    from nflpicker.ratings.power import PowerRatings, TeamPower
    from nflpicker.sim.season import anchor_to_market

    teams = {t: TeamPower(team=t, power=0.0) for t in
             ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")}
    power = PowerRatings(teams=teams)
    ours = dict.fromkeys(teams, 8.5)
    lines = dict.fromkeys(teams, 8.5) | {"AAA": 12.5, "BBB": 4.5}

    out = anchor_to_market(power, ours, lines)
    assert out.teams["AAA"].power > 0        # the market likes them more
    assert out.teams["BBB"].power < 0        # and likes them less
    assert out.teams["CCC"].power == 0       # no disagreement, no move
    # Half the gap, in points: (12.5 - 8.5) * 0.5 * 2.0
    assert abs(out.teams["AAA"].power - 4.0) < 1e-9
    # And the ratings the game cards were drawn from are untouched.
    assert power.teams["AAA"].power == 0.0


def test_a_handful_of_posted_totals_is_a_rumour_not_a_market():
    from nflpicker.ratings.power import PowerRatings, TeamPower
    from nflpicker.sim.season import anchor_to_market

    power = PowerRatings(teams={t: TeamPower(team=t) for t in ("AAA", "BBB")})
    out = anchor_to_market(power, {"AAA": 8.0}, {"AAA": 13.0})
    assert out.teams["AAA"].power == 0.0


# --------------------------------------------------------------- when to refit

def test_a_stale_fit_is_redone_for_one_result(temp_env, monkeypatch):
    """An NFL week lands thirteen games on Sunday, then one on Monday and one
    on Thursday. A threshold only a Sunday can clear meant a Monday night
    result sat unlearned for six days."""
    from nflpicker import db
    from nflpicker.config import get_config
    from nflpicker.pipeline import Pipeline, RefreshResult

    db.connect()
    pipe = Pipeline()
    monkeypatch.setattr(pipe, "demo", False)
    monkeypatch.setattr(pipe, "_retrain", lambda completed, new: f"refit on {completed}")

    for gid in ("g1", "g2"):
        db.execute("INSERT OR REPLACE INTO games(game_id, season, week, season_type, "
                   "home, away, status, updated_at) "
                   "VALUES(?, 2026, 1, 'REG', 'KC', 'DEN', 'final', ?)",
                   (gid, now_iso()))
    # One of the two was in the last fit, so there is exactly one new result.
    db.set_meta("train:n_games", 1)

    cfg = get_config()
    # Fresh fit, one new game, nowhere near the threshold: wait.
    db.set_meta("train:at", now_iso())
    result = RefreshResult()
    pipe.refresh_train(result)
    assert "waiting for" in _detail(result, "train")

    # Same one game, but the fit is older than the ceiling: refit.
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=cfg.train_max_age_hours + 1)
    db.set_meta("train:at", old.isoformat())
    result = RefreshResult()
    pipe.refresh_train(result)
    assert "refit on 2" in _detail(result, "train")


def test_no_new_results_is_never_a_refit(temp_env, monkeypatch):
    """Staleness alone must not retrain: refitting the same rows on a timer
    burns minutes of CPU to reproduce the model it already had."""
    from nflpicker import db
    from nflpicker.pipeline import Pipeline, RefreshResult

    db.connect()
    pipe = Pipeline()
    monkeypatch.setattr(pipe, "demo", False)
    monkeypatch.setattr(pipe, "_retrain", lambda completed, new: "should not happen")
    db.execute("INSERT OR REPLACE INTO games(game_id, season, week, season_type, "
               "home, away, status, updated_at) "
               "VALUES('g1', 2026, 1, 'REG', 'KC', 'DEN', 'final', ?)", (now_iso(),))
    db.set_meta("train:n_games", 1)
    db.set_meta("train:at", "2020-01-01T00:00:00+00:00")
    result = RefreshResult()
    pipe.refresh_train(result)
    assert "no new results" in _detail(result, "train")


def _detail(result, stage: str) -> str:
    return str((result.stages.get(stage) or {}).get("detail") or "")
