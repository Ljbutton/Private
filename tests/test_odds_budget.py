"""What an Odds API poll actually costs, and how often it is allowed to happen.

Both halves were wrong in the same direction -- towards spending the month's
credits early -- and neither was visible from inside the app, because the only
number it showed was its own undercount.
"""

import datetime as dt

import pytest

from nflpicker import db
from nflpicker.config import get_config
from nflpicker.scheduler import Job, Scheduler
from nflpicker.sources.odds_api import OddsApiSource
from nflpicker.util import now


class _Response:
    def __init__(self, headers=None):
        self.headers = headers or {}


@pytest.mark.parametrize("headers, params, expected", [
    # The provider's own figure for the call that just happened wins.
    ({"x-requests-last": "3"}, {"markets": "h2h,spreads,totals", "regions": "us"}, 3),
    ({"x-requests-last": "6"}, {"markets": "h2h", "regions": "us"}, 6),
    # Without it, one credit per market per region.
    ({}, {"markets": "h2h,spreads,totals", "regions": "us"}, 3),
    ({}, {"markets": "h2h,spreads,totals", "regions": "us,uk"}, 6),
    ({}, {"markets": "h2h", "regions": "us"}, 1),
    # Never zero, whatever the input says.
    ({"x-requests-last": "nonsense"}, {"markets": "h2h,spreads", "regions": "us"}, 2),
    ({}, {}, 1),
])
def test_a_poll_costs_one_credit_per_market_per_region(headers, params, expected):
    """The default markets are h2h,spreads,totals, so every poll costs three.

    Counting HTTP requests instead meant a 500-credit budget was actually spent
    after 167 polls while the guard still believed two thirds remained.
    """
    assert OddsApiSource._cost_of(_Response(headers), params) == expected


def _source(monthly=100, weekly=1000, daily=1000):
    source = OddsApiSource.__new__(OddsApiSource)
    source.budget, source.weekly_budget, source.daily_budget = monthly, weekly, daily
    source.remaining = None
    return source


def test_the_recorded_usage_matches_what_was_spent(temp_env):
    source = _source()
    source._record_call(3)
    source._record_call(3)
    assert source.usage()["used"] == 6
    assert source.usage()["remaining_budget"] == 94


def test_the_daily_ceiling_stops_a_runaway_loop(temp_env):
    """The month-only guard could not see a bug coming.

    A stuck refresh loop spends the whole month in an afternoon and the monthly
    number only reports it afterwards, when the account is already empty.
    """
    source = _source(monthly=480, weekly=120, daily=50)
    assert source.budget_block() is None
    for _ in range(17):                       # 51 credits, three at a time
        source._record_call(3)
    blocked = source.budget_block()
    assert blocked and "daily" in blocked and "50" in blocked
    # The month is nowhere near spent -- the day is what stopped it.
    assert source.usage()["remaining_budget"] > 400


def test_the_weekly_ceiling_binds_before_the_month(temp_env):
    source = _source(monthly=480, weekly=120, daily=10_000)
    for _ in range(41):                       # 123 credits
        source._record_call(3)
    blocked = source.budget_block()
    assert blocked and "weekly" in blocked


def test_the_month_is_still_the_bill(temp_env):
    """Daily and weekly are burst ceilings; the month is what you are billed."""
    source = _source(monthly=30, weekly=10_000, daily=10_000)
    for _ in range(11):
        source._record_call(3)
    blocked = source.budget_block()
    assert blocked and "monthly" in blocked


def test_the_three_windows_count_independently(temp_env):
    source = _source(monthly=480, weekly=120, daily=50)
    source._record_call(3)
    u = source.usage()
    assert u["used"] == u["week_used"] == u["day_used"] == 3
    assert (u["day_budget"], u["week_budget"], u["budget"]) == (50, 120, 480)


def test_odds_poll_once_a_day_when_nothing_is_opening(temp_env):
    """The one feed with a bill attached is asked once a day by default.

    The interval used to be worked out from what was left of the monthly
    allowance, which on the first of the month answers "every few minutes".
    A line moves on a scale of days; the button covers wanting today's number
    right now.
    """
    assert Scheduler.ODDS_INTERVAL_SECONDS == 24 * 3600
    scheduler = Scheduler.__new__(Scheduler)
    assert scheduler.odds_interval() == pytest.approx(24 * 3600)


def _game(game_id, days_out, priced=False):
    when = now() + dt.timedelta(days=days_out)
    db.execute(
        "INSERT INTO games(game_id, season, week, kickoff, home, away, status,"
        " updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (game_id, 2026, 3, when.isoformat(), "KC", "BUF", "scheduled",
         now().isoformat()),
    )
    if priced:
        db.execute(
            "INSERT INTO consensus(game_id, captured_at) VALUES(?,?)",
            (game_id, now().isoformat()),
        )


def test_an_imminent_unpriced_game_is_worth_polling_for(temp_env):
    """An opening line exists once, and closing-line value is measured against
    it. A number first seen on Saturday is not where the market started."""
    scheduler = Scheduler.__new__(Scheduler)
    _game("far-off", 90)
    assert scheduler.odds_interval() == pytest.approx(24 * 3600), (
        "books do not price week 15 in September; that is not a pending open")

    _game("this-week", 3)
    assert scheduler.odds_interval() == pytest.approx(20 * 60)


def test_the_opening_line_exception_has_a_daily_ceiling(temp_env):
    """What made this unaffordable was that it had no end.

    At a five-minute floor with no ceiling, a game the feed simply never
    prices leaves the exception switched on for ever and spends the month in
    a week.
    """
    scheduler = Scheduler.__new__(Scheduler)
    _game("this-week", 3)
    for _ in range(Scheduler.OPENING_LINE_POLLS_PER_DAY):
        assert scheduler.odds_interval() == pytest.approx(20 * 60)
        db.execute(
            "INSERT INTO fetch_log(source, ok, ts, duration_ms, detail) "
            "VALUES('odds', 1, ?, 10, '')", (now().isoformat(),))
    assert scheduler.odds_interval() == pytest.approx(24 * 3600), (
        "past the day's allowance the ordinary cadence resumes")


def test_a_priced_game_is_not_waiting_on_an_open(temp_env):
    scheduler = Scheduler.__new__(Scheduler)
    _game("this-week", 3, priced=True)
    assert scheduler.odds_interval() == pytest.approx(24 * 3600)


def test_a_slower_setting_still_wins(temp_env, monkeypatch):
    """A day is a floor, not a cap: someone rationing a small allowance across
    a season can still ask for less than that and be listened to."""
    import dataclasses

    import nflpicker.scheduler as scheduler_module

    cfg = dataclasses.replace(get_config(), refresh_odds=72 * 3600.0)
    monkeypatch.setattr(scheduler_module, "get_config", lambda: cfg)
    scheduler = Scheduler.__new__(Scheduler)
    assert scheduler.odds_interval() == pytest.approx(72 * 3600)


def test_opening_the_app_does_not_spend_a_credit(temp_env, monkeypatch):
    """Launching is not a request for the lines.

    Every job fired the moment the scheduler started, so opening the app spent
    three credits before the window had drawn -- and opening it four times in
    an evening spent twelve on a line that had not moved.
    """
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.jobs = {"odds": None, "scores": None}
    job = Job(name="odds", stages=["odds"], interval=60.0)

    class _Pipeline:
        def __init__(self, since):
            self.since = since

        def last_success(self, stage):
            assert stage == "odds"
            return self.since

    # Fetched an hour ago: the rest of the day is still to wait.
    scheduler.pipeline = _Pipeline(3600.0)
    assert scheduler.startup_wait(job) == pytest.approx(23 * 3600, abs=30)

    # Fetched two days ago, or never: go and get it now.
    scheduler.pipeline = _Pipeline(2 * 24 * 3600.0)
    assert scheduler.startup_wait(job) < 60
    scheduler.pipeline = _Pipeline(None)
    assert scheduler.startup_wait(job) < 60


def test_a_free_feed_still_starts_immediately(temp_env):
    """Only the metered job waits. Scores must be on screen at launch."""
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.jobs = {"odds": None, "scores": None}

    class _Never:
        def last_success(self, stage):
            return 0.0                      # fetched a moment ago

    scheduler.pipeline = _Never()
    assert scheduler.startup_wait(
        Job(name="scores", stages=["scores"], interval=60.0)) < 60
