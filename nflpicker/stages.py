"""The registry of refresh stages.

Adding a data source used to mean editing four files: a fetch method on the
pipeline, a job in the scheduler, an interval in the config, and the stage list
in two places. That is the kind of spread where a source ends up half-wired —
fetched and stored but never actually reaching the model, which has now happened
twice in this project (the play-by-play features, and temperature and wind).

So a stage declares itself once, here, and the pipeline and scheduler both read
from this list. To add a source:

1. Write an adapter under ``nflpicker/sources/`` that returns plain dicts.
2. Add a ``refresh_<name>`` method to the pipeline that fetches and stores.
3. Register it below with an interval and a one-line description.

Anything that needs to reach the model also needs a line in ``recompute`` and a
test that the feature is populated — the registry gets the data in, but only
that test proves it is used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Config


@dataclass(frozen=True)
class Stage:
    """One refreshable source."""

    name: str
    description: str
    # Seconds between polls, read from config so it stays user-tunable.
    interval: Callable[[Config], float]
    # Whether this stage runs at all for the current configuration.
    enabled: Callable[[Config], bool] = field(default=lambda cfg: True)
    # Scheduler jobs exist only for stages that should poll on their own.
    scheduled: bool = True
    # Stages that feed the model and therefore precede a recompute.
    feeds_model: bool = False

    def method_name(self) -> str:
        return f"refresh_{self.name}"


STAGES: tuple[Stage, ...] = (
    Stage(
        name="schedule",
        description="Games, scores and live in-game state",
        interval=lambda cfg: cfg.refresh_scores,
        feeds_model=True,
    ),
    Stage(
        name="odds",
        description="Sportsbook lines across every book",
        interval=lambda cfg: cfg.refresh_odds,
        feeds_model=True,
    ),
    Stage(
        name="prediction_markets",
        description="Polymarket and Kalshi prices, shown for comparison",
        interval=lambda cfg: cfg.refresh_prediction_markets,
        enabled=lambda cfg: cfg.prediction_markets_enabled,
    ),
    Stage(
        name="weather",
        description="Forecast at kickoff for outdoor games",
        interval=lambda cfg: cfg.refresh_weather,
        feeds_model=True,
    ),
    Stage(
        name="news",
        description="Headlines and injury reports",
        interval=lambda cfg: cfg.refresh_news,
        feeds_model=True,
    ),
    Stage(
        name="stats",
        description="Play-by-play efficiency, team detail and depth charts",
        interval=lambda cfg: cfg.refresh_stats,
        feeds_model=True,
    ),
    # Not a source: the analytical pass that turns everything above into
    # ratings, projections and picks. It never polls on its own.
    Stage(
        name="recompute",
        description="Ratings, projections, picks and grading",
        interval=lambda cfg: cfg.refresh_scores,
        scheduled=False,
    ),
)

STAGES_BY_NAME: dict[str, Stage] = {stage.name: stage for stage in STAGES}


def stage_names(config: Config | None = None, *, scheduled_only: bool = False) -> list[str]:
    """Stage names in run order, filtered to what this configuration enables."""
    return [
        stage.name
        for stage in STAGES
        if (config is None or stage.enabled(config))
        and (not scheduled_only or stage.scheduled)
    ]


def describe() -> list[dict]:
    """For the CLI and the interface: what exists and what it is for."""
    return [
        {"name": s.name, "description": s.description,
         "scheduled": s.scheduled, "feeds_model": s.feeds_model}
        for s in STAGES
    ]
