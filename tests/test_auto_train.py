"""Retraining on a schedule is the one job that can make the app worse.

Every test here is about a guard rather than a feature: the point is that an
unattended fit cannot quietly replace a working model with a broken one.
"""

import pytest

from nflpicker import db
from nflpicker.config import reset_config
from nflpicker.pipeline import Pipeline, RefreshResult
from nflpicker.stages import STAGES_BY_NAME


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("NFLPICKER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NFLPICKER_DEMO", raising=False)
    reset_config()
    db.close_all()
    db.connect()
    yield Pipeline(demo=False)
    db.close_all()
    reset_config()


def _game(gid, status):
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, home, away, status, updated_at) "
        "VALUES(?,2026,1,'KC','BUF',?,'2026-09-15T00:00:00Z')",
        (gid, status),
    )


def test_the_stage_is_registered_and_off_in_demo():
    stage = STAGES_BY_NAME["train"]
    assert stage.scheduled is True

    class Cfg:
        train_auto, demo, refresh_train = True, True, 86400

    assert stage.enabled(Cfg()) is False          # demo data must never train
    Cfg.demo = False
    assert stage.enabled(Cfg()) is True
    Cfg.train_auto = False
    assert stage.enabled(Cfg()) is False


def test_a_live_slate_defers(pipeline, monkeypatch):
    """Training holds the refresh lock for minutes. Doing that mid-game would
    stall the score poll exactly when it matters."""
    _game("live", "in_progress")
    called = []
    monkeypatch.setattr(pipeline, "_retrain", lambda *a: called.append(a))

    result = RefreshResult()
    pipeline.refresh_train(result)

    assert not called
    assert "in progress" in result.stages["train"]["detail"]


def test_it_waits_for_a_week_of_results(pipeline, monkeypatch):
    for i in range(20):
        _game(f"g{i}", "final")
    db.set_meta("train:n_games", 15)          # only five new since the last fit
    called = []
    monkeypatch.setattr(pipeline, "_retrain", lambda *a: called.append(a))

    result = RefreshResult()
    pipeline.refresh_train(result)

    assert not called
    assert "waiting for" in result.stages["train"]["detail"]


def test_enough_new_results_triggers_a_fit(pipeline, monkeypatch):
    for i in range(40):
        _game(f"g{i}", "final")
    db.set_meta("train:n_games", 20)          # twenty new, past the threshold
    monkeypatch.setattr(pipeline, "_retrain", lambda completed, new: f"fit {completed}/{new}")

    result = RefreshResult()
    pipeline.refresh_train(result)

    assert result.stages["train"]["detail"] == "fit 40/20"


def test_the_first_fit_does_not_wait(pipeline, monkeypatch):
    """With no recorded previous fit there is no delta to compare, and an app
    that has never trained should not sit at power-only for a week."""
    for i in range(3):
        _game(f"g{i}", "final")
    monkeypatch.setattr(pipeline, "_retrain", lambda completed, new: "fitted")

    result = RefreshResult()
    pipeline.refresh_train(result)

    assert result.stages["train"]["detail"] == "fitted"


def test_a_worse_model_is_rejected(pipeline, monkeypatch, tmp_path):
    """The guard that matters: an upstream break would otherwise replace a good
    model with a broken one overnight, reporting only a new timestamp."""
    import nflpicker.ml.train as train_module

    monkeypatch.setattr(train_module, "load_report",
                        lambda *a, **k: {"blind": {"margin_mae": 10.5}})

    class Report:
        @staticmethod
        def to_dict():
            return {"n_games": 100, "blind": {"margin_mae": 13.9,
                                              "market_margin_mae": 10.2}}

    import pandas as pd

    import nflpicker.ml.dataset as dataset_module
    monkeypatch.setattr(dataset_module, "from_nflverse",
                        lambda *a, **k: pd.DataFrame({"x": [1]}))
    monkeypatch.setattr(train_module, "train", lambda *a, **k: Report())

    detail = pipeline._retrain(100, 20)

    assert "rejected" in detail
    assert "13.9" in detail and "10.5" in detail
    # Nothing was promoted, so the next run still sees no recorded fit.
    assert db.get_meta("train:n_games", None) is None


def test_an_improved_model_is_promoted(pipeline, monkeypatch):
    import pandas as pd

    import nflpicker.ml.dataset as dataset_module
    import nflpicker.ml.train as train_module

    monkeypatch.setattr(train_module, "load_report",
                        lambda *a, **k: {"blind": {"margin_mae": 10.9}})
    monkeypatch.setattr(dataset_module, "from_nflverse",
                        lambda *a, **k: pd.DataFrame({"x": [1]}))

    written = {}

    def fake_train(frame, *, model_dir=None, **kwargs):
        for name in ("models.joblib", "training_report.json"):
            (model_dir / name).write_text("x")
        written["dir"] = model_dir

        class R:
            @staticmethod
            def to_dict():
                return {"n_games": 200, "blind": {"margin_mae": 10.4,
                                                  "market_margin_mae": 10.2}}
        return R()

    monkeypatch.setattr(train_module, "train", fake_train)

    detail = pipeline._retrain(200, 20)

    assert "refit on 200 games" in detail
    assert db.get_meta("train:n_games", 0) == 200
    assert (pipeline.config.model_dir / "models.joblib").exists()
    # The scratch directory is cleaned up whichever way the fit went.
    assert not written["dir"].exists()


def test_going_live_removes_demo_rows(pipeline):
    """Demo games live in the same tables as real ones, told apart only by an
    id prefix. Nothing removed them, so a data directory that had ever been
    opened in demo mode kept a synthetic slate mixed into the real board."""
    _game("demo-2026-01-DAL-PHI", "final")
    _game("401671789", "final")               # a real ESPN id
    db.execute(
        "INSERT INTO predictions(game_id, captured_at, model_version, margin_home,"
        " total_points, home_win_prob) VALUES('demo-2026-01-DAL-PHI','t','v',1,2,0.5)")

    removed = pipeline.purge_demo_data()

    assert removed >= 2
    ids = [r["game_id"] for r in db.query("SELECT game_id FROM games")]
    assert ids == ["401671789"]
    assert db.query("SELECT game_id FROM predictions") == []


def test_the_purge_covers_every_table_keyed_on_a_game():
    """Discovered from the schema, not a hardcoded list -- which is the kind
    that goes stale the next time a game-keyed table is added."""
    import inspect

    from nflpicker.pipeline import Pipeline

    source = inspect.getsource(Pipeline.purge_demo_data)
    assert "sqlite_master" in source
    assert "PRAGMA table_info" in source
