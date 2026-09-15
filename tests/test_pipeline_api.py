"""End-to-end: a full refresh in demo mode, then every API endpoint."""

import pytest
from fastapi.testclient import TestClient

from nflpicker import db


@pytest.fixture()
def booted(pipeline):
    pipeline.refresh(["schedule", "odds", "news", "recompute"])
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

    from nflpicker.sources.polymarket import VENUE

    booted.recompute()
    row = db.query_one(
        "SELECT payload FROM pick_history WHERE contest = 'ats' "
        "ORDER BY captured_at DESC LIMIT 1"
    )
    edges = json.loads(row["payload"])["edges"] if row else []
    books = {e.get("book") for e in edges}
    assert VENUE not in books, f"sportsbook edges priced at {VENUE}: {books}"


def test_cross_market_edges_are_priced_against_the_consensus(booted):
    import json

    booted.recompute()
    row = db.query_one(
        "SELECT payload FROM pick_history WHERE contest = 'crossmarket' "
        "ORDER BY captured_at DESC LIMIT 1"
    )
    edges = json.loads(row["payload"])["edges"] if row else []
    for e in edges:
        assert e["n_books"] >= 3, "consensus must rest on several books"
        assert e["depth"] > 0, "an edge with no depth is not tradeable"
        assert e["max_stake"] <= e["depth"] + 1e-6
        assert abs((e["fair_prob"] - e["venue_price"]) - e["gap"]) < 1e-6
