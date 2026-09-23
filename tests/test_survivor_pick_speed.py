"""Recording a survivor pick redoes the plan, not the world.

The button that spends a team sat on the board beside the game, which is the
right place for it and made its cost obvious: every press ran a full
recompute -- Elo over every game ever played, the model over the whole
history, twenty thousand season simulations and a grading pass -- for a change
that touches one thing. The only consumer of the used-teams list is the
optimiser, which must not offer a team back once it has gone.
"""

import pytest
from fastapi.testclient import TestClient

from nflpicker import db


@pytest.fixture()
def client(temp_env):
    from nflpicker.api import create_app

    with TestClient(create_app(start_scheduler=False, bootstrap=False)) as c:
        yield c


def _schedule(season=2026, week=3):
    """Three future weeks, each a pair of games, with a stored prediction."""
    db.execute("DELETE FROM games")
    db.execute("DELETE FROM predictions")
    pairs = [("KC", "DEN"), ("BUF", "NYJ"), ("PHI", "DAL"), ("SF", "SEA")]
    for wk in (week, week + 1, week + 2):
        for i, (home, away) in enumerate(pairs):
            gid = f"g{wk}-{i}"
            db.execute(
                "INSERT INTO games(game_id, season, week, season_type, kickoff,"
                " home, away, status, updated_at) "
                "VALUES(?,?,?,'REG',?,?,?,'scheduled',?)",
                (gid, season, wk, f"2026-10-{wk:02d}T17:00:00Z", home, away,
                 "2026-10-01T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO predictions(game_id, captured_at, model_version,"
                " margin_home, total_points, home_win_prob) "
                "VALUES(?,?,'t',3.0,44.0,?)",
                (gid, "2026-10-01T00:00:00Z", 0.80 - i * 0.05),
            )


def test_the_plan_is_redone_without_a_recompute(pipeline, temp_env, monkeypatch):
    """The narrow path produces a plan, and never reaches the analytical core."""
    _schedule()
    monkeypatch.setattr(pipeline, "current_week", lambda season=None: 3)

    def _boom(*args, **kwargs):
        raise AssertionError("recompute must not run to record a pick")

    monkeypatch.setattr(type(pipeline), "recompute", _boom)
    plan = pipeline.replan_survivor(2026)
    assert plan.get("path"), "the optimiser still produces a run"


def test_a_spent_team_leaves_the_plan(pipeline, temp_env, monkeypatch, client):
    """The one thing the pick actually changes has to still change."""
    _schedule()
    monkeypatch.setattr(pipeline, "current_week", lambda season=None: 3)

    before = pipeline.replan_survivor(2026)
    taken = before["path"][0]["team"]

    # Spend that team in a later week: the plan must stop offering it.
    db.set_meta("survivor_used_teams", [taken])
    after = pipeline.replan_survivor(2026)
    assert taken not in [e["team"] for e in after["path"]], (
        "a team already spent cannot appear in the remaining run")


def test_the_endpoint_writes_the_pick_and_the_plan(pipeline, temp_env,
                                                   monkeypatch, client):
    """End to end, through the button the board actually presses."""
    _schedule()
    monkeypatch.setattr(pipeline, "current_week", lambda season=None: 3)
    db.set_meta("survivor_picks_by_week", {})

    out = client.post("/api/survivor/pick",
                      json={"season": 2026, "week": 4, "team": "KC"}).json()
    assert out["ok"] and out["team"] == "KC"
    assert out["picks"]["4"] == "KC"
    assert db.get_meta("survivor_used_teams", []) == ["KC"]

    # And releasing it puts the team back.
    out = client.post("/api/survivor/pick",
                      json={"season": 2026, "week": 4, "team": None}).json()
    assert out["team"] is None
    assert db.get_meta("survivor_used_teams", []) == []
