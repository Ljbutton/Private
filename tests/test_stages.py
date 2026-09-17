"""The stage registry.

Its purpose is that adding a source cannot leave it half-wired, so these check
the wiring itself rather than any one source.
"""

import pytest

from nflpicker.config import get_config
from nflpicker.pipeline import Pipeline
from nflpicker.scheduler import Scheduler
from nflpicker.stages import STAGES, STAGES_BY_NAME, describe, stage_names
from nflpicker.util import now_iso


def test_every_stage_has_a_handler_on_the_pipeline(temp_env):
    """A registered stage with no method would report 'no handler' at runtime
    instead of failing loudly here."""
    pipeline = Pipeline()
    for stage in STAGES:
        assert hasattr(pipeline, stage.method_name()), stage.name


def test_recompute_runs_last(temp_env):
    """It consumes what every other stage produces, so order is load-bearing."""
    assert stage_names()[-1] == "recompute"


def test_recompute_is_not_polled_on_its_own(temp_env):
    assert STAGES_BY_NAME["recompute"].scheduled is False
    assert "recompute" not in Scheduler(Pipeline()).jobs


def test_the_scheduler_builds_its_jobs_from_the_registry(temp_env):
    jobs = set(Scheduler(Pipeline()).jobs)
    expected = set(stage_names(get_config(), scheduled_only=True))
    assert jobs == expected


def test_a_disabled_stage_is_neither_run_nor_polled(temp_env, monkeypatch):
    from nflpicker import config

    monkeypatch.setenv("PREDICTION_MARKETS_ENABLED", "0")
    config.reset_config()
    cfg = config.get_config()
    assert "prediction_markets" not in stage_names(cfg)
    assert "prediction_markets" not in Scheduler(Pipeline()).jobs


def test_intervals_are_positive_and_tunable(temp_env):
    cfg = get_config()
    for stage in STAGES:
        assert stage.interval(cfg) > 0, stage.name


def test_names_are_unique_and_descriptions_present():
    names = [s.name for s in STAGES]
    assert len(names) == len(set(names))
    assert all(s.description for s in STAGES)


def test_describe_matches_the_registry():
    assert [d["name"] for d in describe()] == [s.name for s in STAGES]


def test_refresh_honours_an_explicit_stage_list(pipeline):
    result = pipeline.refresh(["schedule"])
    assert set(result.stages) == {"schedule"}


def test_one_failing_stage_does_not_stop_the_others(pipeline, monkeypatch):
    """Stage isolation is the whole reason a dead feed cannot cost you odds."""
    def boom(self, result):
        raise RuntimeError("feed down")

    monkeypatch.setattr(Pipeline, "refresh_news", boom)
    result = pipeline.refresh(["schedule", "news", "odds"])
    assert result.stages["news"]["ok"] is False
    assert "feed down" in result.stages["news"]["detail"]
    assert result.stages["schedule"]["ok"] is True
    assert result.stages["odds"]["ok"] is True


def test_an_unknown_stage_name_is_ignored_rather_than_crashing(pipeline):
    result = pipeline.refresh(["not_a_stage"])
    assert result.stages == {}


@pytest.mark.usefixtures("temp_env")
def test_stages_that_feed_the_model_are_marked():
    feeding = {s.name for s in STAGES if s.feeds_model}
    assert {"schedule", "odds", "weather", "news", "stats"} <= feeding
    # A comparison-only source must not claim to feed the model.
    assert "prediction_markets" not in feeding


def test_a_plain_refresh_skips_what_is_not_due(pipeline, temp_env):
    """Asking for everything means "catch up on what is due", not "re-fetch the
    world". Before this, each press of the refresh button and each launch of
    the app spent three Odds API credits on a line that had not moved, and
    re-downloaded the nflverse play-by-play release.
    """
    from nflpicker import db

    db.execute(
        "INSERT INTO fetch_log(source, ok, ts, duration_ms, detail) "
        "VALUES('odds', 1, ?, 10, 'seeded')", (now_iso(),))

    called: list[str] = []
    original = pipeline.refresh_odds

    def spy(result, *, force=False):
        called.append("odds")
        return original(result, force=force)

    pipeline.refresh_odds = spy
    result = pipeline.refresh(["odds", "recompute"])
    assert called == ["odds"], "naming a stage forces it"

    called.clear()
    result = pipeline.refresh()
    assert called == [], "a plain refresh leaves a stage inside its interval alone"
    assert result.stages["odds"]["ok"] is True, "skipped is not failed"
    assert result.stages["odds"]["skipped"] is True
    assert "already current" in result.stages["odds"]["detail"]


def test_a_stale_stage_still_runs_on_a_plain_refresh(pipeline, temp_env):
    """The interval is a floor, not a mute button."""
    from datetime import timedelta

    from nflpicker import db
    from nflpicker.util import now

    long_ago = (now() - timedelta(days=3)).isoformat()
    db.execute(
        "INSERT INTO fetch_log(source, ok, ts, duration_ms, detail) "
        "VALUES('odds', 1, ?, 10, 'seeded')", (long_ago,))

    called: list[str] = []
    original = pipeline.refresh_odds
    pipeline.refresh_odds = lambda result, *, force=False: (
        called.append("odds"), original(result, force=force))[1]

    pipeline.refresh()
    assert called == ["odds"]


def test_recompute_is_never_skipped(pipeline, temp_env):
    """It is local arithmetic over whatever the stages just stored, and seeing
    its output is the reason a refresh was asked for at all."""
    from nflpicker import db
    from nflpicker.stages import STAGES_BY_NAME

    db.execute(
        "INSERT INTO fetch_log(source, ok, ts, duration_ms, detail) "
        "VALUES('recompute', 1, ?, 10, 'seeded')", (now_iso(),))
    assert STAGES_BY_NAME["recompute"].always is True

    called: list[str] = []
    pipeline.refresh_recompute = lambda result: called.append("recompute")
    pipeline.refresh()
    assert called == ["recompute"]


def test_no_network_stage_is_marked_always(temp_env):
    """`always` exists for local work. A stage that costs a network call, or
    money, has an interval for a reason -- marking one always would restore
    exactly the bug the interval check was added to fix."""
    from nflpicker.stages import STAGES

    always = {s.name for s in STAGES if s.always}
    assert always == {"recompute"}, f"unexpected always-on stages: {always}"
