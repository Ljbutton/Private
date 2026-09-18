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

    # Books post lines about a week out. Beyond that a game having no price is
    # the normal state of the world, not a market we are waiting on.
    OPENING_LINE_HORIZON_DAYS = 8

    @classmethod
    def _awaiting_opening_lines(cls) -> bool:
        """Are there *imminent* games the market has not priced for us yet?

        The horizon is the whole point. Without it this asked whether any game
        in the rest of the season lacked a line, which in week 2 is most of the
        season and stays true until December -- so the budget-aware interval
        was bypassed essentially always and odds polled at the floor for
        months. The exception exists to catch an opening number the week it
        appears, and a game sixteen weeks out has no opening number to miss.
        """
        horizon = (now() + dt.timedelta(days=cls.OPENING_LINE_HORIZON_DAYS)).isoformat()
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM games g WHERE g.status = 'scheduled' "
            "AND g.kickoff IS NOT NULL AND g.kickoff > ? AND g.kickoff <= ? "
            "AND NOT EXISTS (SELECT 1 FROM consensus c WHERE c.game_id = g.game_id)",
            (now_iso(), horizon),
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

    def inputs_fingerprint(self) -> tuple:
        """Everything a recompute would read, as one comparable value.

        Cheap: four aggregates over indexed columns, against a recompute that
        replays twenty thousand seasons.
        """
        games = db.query_one(
            "SELECT COUNT(*) n, MAX(updated_at) u, "
            " SUM(COALESCE(home_score, 0) + COALESCE(away_score, 0)) pts, "
            " SUM(status = 'final') fin FROM games") or {}
        odds = db.query_one(
            "SELECT COUNT(*) n, MAX(captured_at) c FROM odds_snapshots") or {}
        stats = db.query_one(
            "SELECT COUNT(*) n, MAX(updated_at) u FROM team_game_stats") or {}
        injuries = db.query_one("SELECT COUNT(*) n FROM injuries") or {}
        return (
            games.get("n"), games.get("u"), games.get("pts"), games.get("fin"),
            odds.get("n"), odds.get("c"),
            stats.get("n"), stats.get("u"), injuries.get("n"),
        )

    # A ceiling on how long derived state may go untouched even when nothing
    # upstream has moved. Not a poll: on a quiet Tuesday the inputs do not
    # change for hours and this is the only thing that runs.
    RECOMPUTE_CEILING_SECONDS = 6 * 3600

    def recompute_overdue(self) -> bool:
        """Whether a recompute is due on time rather than on new data.

        The fingerprint below is the right test for "would this produce a
        different answer", with one exception it cannot see: some of what a
        recompute does is keyed to the calendar rather than to the inputs --
        the weekly ranking cut most of all, which falls due on a Wednesday
        morning when nothing has arrived since Monday night. Without a ceiling
        the cut would wait for the next score to land.
        """
        last = db.get_meta("last_recompute") or ""
        stamp = to_utc(last) if last else None
        if stamp is None:
            return True
        return (now() - stamp).total_seconds() >= self.RECOMPUTE_CEILING_SECONDS

    async def run_once(self, job: Job) -> None:
        """Run a job's stages, then recompute only if they brought anything.

        The recompute is the expensive half of a refresh -- it retrains
        nothing, but it does replay twenty thousand seasons and rewrite every
        projection -- and it used to run after *every* job on *every* tick.
        Six jobs on their own timers meant the app was recomputing several
        times an hour to reach the same answer, which is what "refreshing more
        often than it should" looked like from outside: fans, a busy status
        line, and numbers that never actually changed.

        A stage that fetched nothing new cannot change the output of a
        deterministic pass over the same inputs, so the inputs are fingerprinted
        instead. Scores, schedule, lines, team stats and injuries are what a
        recompute reads; if none of them moved, the answer it would produce is
        the one already on screen.
        """
        assert self._lock is not None
        async with self._lock:
            before = await asyncio.to_thread(self.inputs_fingerprint)
            result = await asyncio.to_thread(self.pipeline.refresh, list(job.stages))
            after = await asyncio.to_thread(self.inputs_fingerprint)
            if after != before or await asyncio.to_thread(self.recompute_overdue):
                await asyncio.to_thread(self.pipeline.refresh, ["recompute"])
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
            "jobs": [
                {**job.to_dict(), "next_interval_seconds": round(self.interval_for(job))}
                for job in self.jobs.values()
            ],
        }
