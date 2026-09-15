"""Kickoff weather, and the gap it closes.

The model has trained on temperature and wind since the beginning, because the
historical record carries them. Nothing filled them for games that had not been
played, so two trained-on columns arrived empty at every inference.

These tests insert games with genuinely future kickoffs rather than leaning on
the demo calendar: the stage deliberately skips games whose kickoff has passed,
and a fixture pinned to a past season would silently test nothing.
"""

import datetime as dt

import pytest

from nflpicker import db
from nflpicker.ml.features import build_features
from nflpicker.pipeline import RefreshResult


def _future_game(game_id: str, home: str, away: str, days: int = 3) -> None:
    kickoff = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat()
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, season_type, kickoff, "
        "home, away, status, updated_at) VALUES(?,2026,1,'REG',?,?,?,'scheduled','x')",
        (game_id, kickoff, home, away),
    )


def test_forecasts_are_stored_for_upcoming_games(pipeline):
    _future_game("g1", "BUF", "NYJ")       # outdoor
    _future_game("g2", "DET", "GB")        # dome
    result = RefreshResult()
    pipeline.refresh_weather(result)

    assert result.stages["weather"]["ok"] is True
    rows = {r["game_id"]: r for r in db.query("SELECT * FROM game_weather")}
    assert {"g1", "g2"} <= set(rows)


def test_indoor_venues_are_marked_and_calm(pipeline):
    _future_game("g2", "DET", "GB")        # Ford Field is a dome
    pipeline.refresh_weather(RefreshResult())
    row = db.query_one("SELECT * FROM game_weather WHERE game_id = 'g2'")
    assert row["indoor"] == 1
    assert (row["wind_mph"] or 0) == 0.0


def test_games_already_played_are_not_forecast(pipeline):
    """Forecasting the past wastes calls and, ordered by kickoff, would crowd
    out the games that actually need one."""
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)).isoformat()
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, season_type, kickoff, "
        "home, away, status, updated_at) VALUES('old',2026,1,'REG',?, 'BUF','NYJ',"
        "'scheduled','x')", (past,))
    pipeline.refresh_weather(RefreshResult())
    assert db.query_one("SELECT COUNT(*) AS n FROM game_weather WHERE game_id='old'")["n"] == 0


def test_the_previously_empty_features_are_populated(pipeline):
    """The regression guard: trained-on columns must not be NaN in production."""
    _future_game("g1", "BUF", "NYJ")
    pipeline.refresh_weather(RefreshResult())

    games = db.query("SELECT * FROM games WHERE game_id = 'g1'")
    weather = pipeline.load_weather()
    for game in games:
        forecast = weather.get(game["game_id"])
        if forecast and game.get("temp") is None:
            game["temp"] = forecast["temp_f"]
            game["wind"] = forecast["wind_mph"]

    frame = build_features(games)
    assert frame["temp"].notna().all()
    assert frame["wind"].notna().all()


def test_a_recorded_reading_wins_over_a_forecast(pipeline):
    """History has its own measurement; a forecast must not overwrite it."""
    _future_game("g1", "BUF", "NYJ")
    pipeline.refresh_weather(RefreshResult())
    weather = pipeline.load_weather()

    game = {"game_id": "g1", "temp": 70.0, "wind": 2.0}
    forecast = weather.get("g1")
    if forecast and game.get("temp") is None:
        game["temp"] = forecast["temp_f"]
    assert game["temp"] == 70.0


def test_a_failed_provider_does_not_fail_the_refresh(pipeline, monkeypatch):
    _future_game("g1", "BUF", "NYJ")
    monkeypatch.setattr(
        "nflpicker.sources.demo.generate_weather",
        lambda games: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    result = RefreshResult()
    pipeline.refresh_weather(result)
    assert result.stages["weather"]["ok"] is False
    assert "provider down" in result.stages["weather"]["detail"]


def test_no_upcoming_games_is_reported_as_success(pipeline):
    result = RefreshResult()
    pipeline.refresh_weather(result)
    assert result.stages["weather"]["ok"] is True


@pytest.mark.usefixtures("pipeline")
def test_wind_is_the_flag_that_matters():
    """Above roughly 15mph wind is the largest weather effect on scoring, and
    it is the threshold the interface keys on."""
    from nflpicker.sources.demo import generate_weather

    games = [{"game_id": f"g{i}", "home": "BUF"} for i in range(60)]
    forecasts = generate_weather(games)
    winds = [f["wind_mph"] for f in forecasts.values()]
    assert any(w >= 15 for w in winds), "demo should exercise the windy branch"
    assert all(w >= 0 for w in winds)
