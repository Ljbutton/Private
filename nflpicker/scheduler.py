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
import datetime as dt
import logging
from dataclasses import dataclass, field

from . import db
from .config import get_config
from .pipeline import Pipeline
from .util import UTC, now, now_iso, to_utc

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
    # One fetch a day, at a fixed local time.
    #
    # Every other feed here is free to poll. The odds are a metered monthly
    # allowance and each fetch spends three of it, so the schedule is the one
    # decision with a bill attached -- and it kept finding reasons to spend.
    # First it was derived from what was left of the allowance, which on the
    # first of the month answers "every few minutes". Then it was a day, with
    # an exception that shrank the gap to twenty minutes whenever a game
    # inside a week had no price yet -- which is most of any Tuesday, so the
    # exception was the rule and the app was polling all day again.
    #
    # A wall-clock time has neither failure mode. It cannot drift, it cannot
    # be triggered into firing more often, and it is checkable: the app can
    # say exactly when the next one is, which no interval-since-last-fetch
    # scheme can. 11:30 central is late enough that the week's lines are up
    # and early enough to be hours ahead of any kickoff.
    ODDS_HOUR = 11
    ODDS_MINUTE = 30
    ODDS_TZ = "America/Chicago"

    @classmethod
    def odds_zone(cls) -> dt.tzinfo:
        """Central time, falling back to a fixed offset without tzdata.

        A frozen build on a machine with no zoneinfo database must not lose
        the schedule entirely; UTC-6 is central standard time, which is wrong
        by an hour through the summer and right through the season.
        """
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(cls.ODDS_TZ)
        except Exception:                                     # noqa: BLE001
            return dt.timezone(dt.timedelta(hours=-6))

    @classmethod
    def next_odds_run(cls, after: dt.datetime | None = None) -> dt.datetime:
        """The next 11:30 central, in UTC."""
        zone = cls.odds_zone()
        local = (after or now()).astimezone(zone)
        target = local.replace(hour=cls.ODDS_HOUR, minute=cls.ODDS_MINUTE,
                               second=0, microsecond=0)
        if target <= local:
            target += dt.timedelta(days=1)
        return target.astimezone(UTC)

    @classmethod
    def last_odds_run(cls, before: dt.datetime | None = None) -> dt.datetime:
        """The most recent 11:30 central that has already passed, in UTC."""
        return cls.next_odds_run(before) - dt.timedelta(days=1)

    def odds_interval(self) -> float:
        """Seconds until the next scheduled fetch.

        A configured interval longer than a day still wins -- someone
        rationing a small allowance across a season asked for less than daily
        and should get it.
        """
        wait = (self.next_odds_run() - now()).total_seconds()
        configured = get_config().refresh_odds
        return max(wait, 0.0) if configured <= 24 * 3600 else configured

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
    def startup_wait(self, job: Job) -> float:
        """How long this job waits before its first run of the session.

        Everything here fired the moment the scheduler started, which is right
        for a feed that costs nothing and wrong for the one that does not:
        opening the app spent three Odds API credits before the window had
        finished drawing, and opening it four times in an evening spent twelve
        on a line that had not moved.

        The metered job answers to the clock instead. It catches up only if
        today's 11:30 has already gone by without a fetch -- opening the app
        at nine in the evening having not opened it all day gets the day's
        lines, and opening it four more times that evening gets nothing,
        because the day's fetch has happened. Otherwise it waits for the next
        one. Read from the fetch log, so closing the app and reopening it does
        not hand the schedule a clean slate.
        """
        # Stagger startup so several jobs do not all fire in the same second.
        stagger = 2 + 3 * list(self.jobs).index(job.name)
        if job.name not in self.METERED_STAGES:
            return stagger
        since = self.pipeline.last_success(job.name)
        if since is None:                      # never fetched: go and get it
            return stagger
        due = self.last_odds_run()             # today's slot, if it has passed
        fetched_at = now() - dt.timedelta(seconds=since)
        if fetched_at < due:
            return stagger                     # missed it; take it now
        return max(stagger, self.odds_interval())

    async def _run_job(self, job: Job) -> None:
        """Run one job forever, sleeping its (possibly dynamic) interval."""
        await asyncio.sleep(self.startup_wait(job))
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
        """Run a job's stages, then let the recompute decide for itself.

        The recompute is the expensive half of a refresh -- it retrains
        nothing, but it does replay twenty thousand seasons and rewrite every
        projection -- and it used to run after *every* job on *every* tick.
        Six jobs on their own timers meant the app was recomputing several
        times an hour to reach the same answer, which is what "refreshing more
        often than it should" looked like from outside: fans, a busy status
        line, and numbers that never actually changed.

        The test for that lived here, and here was the wrong place. This loop
        is off by default; the browser drives refreshes itself through
        /api/refresh, which never reaches this function -- so the guard
        protected a path almost nobody was on while the path everybody was on
        recomputed every single minute. It has moved into `recompute` itself,
        which every caller goes through, and this now just asks for the stage
        and lets it make its own decision.
        """
        assert self._lock is not None
        async with self._lock:
            result = await asyncio.to_thread(self.pipeline.refresh, list(job.stages))
            await asyncio.to_thread(self.pipeline.recompute)
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

    # The season-long play-by-play download, which is most of a slow refresh
    # and cannot have changed since this morning. Left to its own interval even
    # on a forced run.
    SLOW_STAGES = ("stats",)

    # The one stage that costs money. Every other source here is free and can
    # be asked as often as you like; the Odds API is a monthly allowance of a
    # few hundred requests, and each poll spends three of them. So it is never
    # part of "refresh everything" -- it has its own button, which says what it
    # is about to spend, and nothing else can reach it by accident.
    METERED_STAGES = ("odds",)

    async def refresh_now(self, stages: list[str] | None = None, *,
                          full: bool = False) -> dict:
        """Manual refresh, sharing the lock so it cannot overlap a scheduled run.

        `full` is what the button in the corner asks for. Without it a refresh
        means "catch up on whatever is due", and each stage that was polled
        inside its own interval is skipped -- which is right for a timer and
        wrong for a person who has just pressed refresh. Pressing the button
        *is* the request to spend an Odds API credit.

        Everything except the play-by-play download, which is most of the wait
        and is a season's worth of history that has not changed since this
        morning. It keeps its own interval.
        """
        from .stages import stage_names

        if full and not stages:
            stages = [n for n in stage_names(get_config())
                      if n not in self.SLOW_STAGES + self.METERED_STAGES]
        lock = self._lock or asyncio.Lock()
        async with lock:
            result = await asyncio.to_thread(
                self.pipeline.refresh, stages,
                force_odds=bool(full or (stages and "odds" in stages)),
            )
        return result.to_dict()

    def status(self) -> dict:
        return {
            "running": self.running,
            # When the metered feed is next due, so the window can count down
            # to it rather than leaving "once a day" as something you have to
            # take on trust -- which is how it came to be polling all day
            # without anyone being able to see that it was.
            "next_odds_at": self.next_odds_run().isoformat(),
            "jobs": [
                {**job.to_dict(), "next_interval_seconds": round(self.interval_for(job))}
                for job in self.jobs.values()
            ],
        }
