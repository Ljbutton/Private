"""Background auto-refresh.

Different data ages at very different rates, so each source gets its own
interval rather than one global timer: scores matter every few minutes while
games are being played, odds every quarter hour, season-long EPA once a day.

When an Odds API key is present the odds interval is also budget-aware — it
stretches automatically so a 500-request free tier lasts the whole month
instead of running out mid-season.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from . import db
from .config import get_config
from .pipeline import Pipeline
from .util import now, now_iso, to_utc

log = logging.getLogger("nflpicker.scheduler")


@dataclass
class Job:
    name: str
    interval: float
    stages: list[str]
    last_run: str | None = None
    last_ok: bool | None = None
    last_detail: str = ""
    runs: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "interval_seconds": round(self.interval),
            "last_run": self.last_run,
            "last_ok": self.last_ok,
            "last_detail": self.last_detail,
            "runs": self.runs,
        }


@dataclass
class Scheduler:
    pipeline: Pipeline
    jobs: dict[str, Job] = field(default_factory=dict)
    running: bool = False
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        # Jobs come from the stage registry, so a newly registered source is
        # polled without touching this file.
        from .stages import STAGES

        cfg = get_config()
        self.jobs = {
            stage.name: Job(stage.name, stage.interval(cfg), [stage.name])
            for stage in STAGES
            if stage.scheduled and stage.enabled(cfg)
        }

    # ------------------------------------------------------------- intervals
    def odds_interval(self) -> float:
        """Stretch odds polling to fit the remaining monthly request budget.

        With one exception: when games have appeared on the schedule but have no
        line yet, poll at the floor. Opening numbers are the softest of the week
        and they exist only once — a budget-stretched interval can miss the open
        entirely, and the open is precisely what the movement test needs.
        """
        cfg = get_config()
        if self._awaiting_opening_lines():
            return max(300.0, min(cfg.refresh_odds, 600.0))
        if self.pipeline.demo or not cfg.has_odds_key:
            return cfg.refresh_odds
        try:
            from .sources.odds_api import OddsApiSource, days_left_in_month, suggested_interval

            usage = OddsApiSource().usage()
            return max(
                cfg.refresh_odds,
                suggested_interval(usage["remaining_budget"], days_left_in_month()),
            )
        except Exception:  # noqa: BLE001
            return cfg.refresh_odds

    @staticmethod
    def _awaiting_opening_lines() -> bool:
        """Are there upcoming games the market has not priced for us yet?"""
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM games g WHERE g.status = 'scheduled' "
            "AND g.kickoff IS NOT NULL AND g.kickoff > ? "
            "AND NOT EXISTS (SELECT 1 FROM consensus c WHERE c.game_id = g.game_id)",
            (now_iso(),),
        )
        return bool(row and row["n"])

    def scores_interval(self) -> float:
        """Poll scores hard while games are live, gently otherwise."""
        cfg = get_config()
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'in_progress'"
        )
        if row and row["n"]:
            return min(cfg.refresh_scores, 60.0)
        upcoming = db.query_one(
            "SELECT MIN(kickoff) AS k FROM games WHERE status = 'scheduled' AND kickoff IS NOT NULL"
        )
        kickoff = to_utc(upcoming["k"]) if upcoming and upcoming["k"] else None
        if kickoff:
            hours = (kickoff - now()).total_seconds() / 3600.0
            if hours > 24:
                return max(cfg.refresh_scores, 3600.0)
        return cfg.refresh_scores

    def interval_for(self, job: Job) -> float:
        if job.name == "odds":
            return self.odds_interval()
        if job.name == "schedule":
            return self.scores_interval()
        return job.interval

    # ----------------------------------------------------------------- loop
    async def _run_job(self, job: Job) -> None:
        """Run one job forever, sleeping its (possibly dynamic) interval."""
        # Stagger startup so four jobs do not all fire in the same second.
        await asyncio.sleep(2 + 3 * list(self.jobs).index(job.name))
        while self.running:
            try:
                await self.run_once(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a job must never kill the loop
                log.warning("job %s failed: %s", job.name, exc)
                job.last_ok = False
                job.last_detail = str(exc)
            await asyncio.sleep(max(30.0, self.interval_for(job)))

    async def run_once(self, job: Job) -> None:
        """Run a job's stages then recompute, serialised against other jobs."""
        assert self._lock is not None
        async with self._lock:
            result = await asyncio.to_thread(
                self.pipeline.refresh, [*job.stages, "recompute"]
            )
        stage = result.stages.get(job.stages[0], {})
        job.last_run = now_iso()
        job.last_ok = bool(stage.get("ok"))
        job.last_detail = str(stage.get("detail", ""))[:200]
        job.runs += 1
        db.set_meta(f"job:{job.name}", job.to_dict())

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._lock = asyncio.Lock()
        self._tasks = [asyncio.create_task(self._run_job(job)) for job in self.jobs.values()]
        log.info("scheduler started with %d jobs", len(self._tasks))

    async def stop(self) -> None:
        self.running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def refresh_now(self, stages: list[str] | None = None) -> dict:
        """Manual refresh, sharing the lock so it cannot overlap a scheduled run."""
        lock = self._lock or asyncio.Lock()
        async with lock:
            result = await asyncio.to_thread(
                self.pipeline.refresh, stages, force_odds=bool(stages and "odds" in stages)
            )
        return result.to_dict()

    def status(self) -> dict:
        return {
            "running": self.running,
            "jobs": [
                {**job.to_dict(), "next_interval_seconds": round(self.interval_for(job))}
                for job in self.jobs.values()
            ],
        }
