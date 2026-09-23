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


def _game(game_id, days_out, priced=False):
    """A scheduled game, optionally with a price already on it."""
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


def test_the_lines_are_fetched_at_half_past_eleven_central(temp_env):
    """A wall-clock time, not an interval since the last fetch.

    Two schemes came before this and both found reasons to spend. One derived
    the gap from what was left of the monthly allowance, which on the first of
    the month answers "every few minutes". The next was a day with an
    exception: twenty minutes whenever a game inside a week had no price yet,
    which is most of any Tuesday -- so the exception was the rule and it was
    polling all day again.

    A fixed time cannot drift and cannot be triggered into firing more often.
    """
    assert (Scheduler.ODDS_HOUR, Scheduler.ODDS_MINUTE) == (11, 30)

    nxt = Scheduler.next_odds_run()
    local = nxt.astimezone(Scheduler.odds_zone())
    assert (local.hour, local.minute) == (11, 30)
    assert nxt > now(), "always the next one, never one that has gone"
    assert nxt - now() <= dt.timedelta(days=1)


def test_the_schedule_holds_either_side_of_the_clocks_changing(temp_env):
    """11:30 central stays 11:30 central, which is the point of naming a zone.

    Pinned to a UTC offset instead it would drift an hour twice a year, and
    drift in the one job with a bill attached is how you end up fetching twice
    in a day without meaning to.
    """
    zone = Scheduler.odds_zone()
    for moment in (dt.datetime(2026, 1, 15, 3, 0, tzinfo=dt.timezone.utc),
                   dt.datetime(2026, 7, 15, 3, 0, tzinfo=dt.timezone.utc)):
        local = Scheduler.next_odds_run(moment).astimezone(zone)
        assert (local.hour, local.minute) == (11, 30)


def test_the_interval_is_the_wait_until_that_time(temp_env):
    scheduler = Scheduler.__new__(Scheduler)
    wait = scheduler.odds_interval()
    assert 0 <= wait <= 24 * 3600
    assert wait == pytest.approx(
        (Scheduler.next_odds_run() - now()).total_seconds(), abs=5)


def test_nothing_on_the_board_can_make_it_poll_sooner(temp_env):
    """The exception this replaced was open-ended by construction.

    A game the feed simply never prices left it switched on for ever, and an
    unpriced game inside the horizon is the ordinary state of a Tuesday. No
    amount of unpriced football moves the schedule now.
    """
    scheduler = Scheduler.__new__(Scheduler)
    before = scheduler.odds_interval()
    for i in range(6):
        _game(f"unpriced-{i}", 2)
    assert scheduler.odds_interval() == pytest.approx(before, abs=5)


def test_a_slower_setting_still_wins(temp_env, monkeypatch):
    """A day is a floor, not a cap: someone rationing a small allowance across
    a season can still ask for less than that and be listened to."""
    import dataclasses

    import nflpicker.scheduler as scheduler_module

    cfg = dataclasses.replace(get_config(), refresh_odds=72 * 3600.0)
    monkeypatch.setattr(scheduler_module, "get_config", lambda: cfg)
    scheduler = Scheduler.__new__(Scheduler)
    assert scheduler.odds_interval() == pytest.approx(72 * 3600)


def _startup(since: float | None):
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.jobs = {"odds": None, "scores": None}

    class _Pipeline:
        def last_success(self, stage):
            assert stage == "odds"
            return since

    scheduler.pipeline = _Pipeline()
    return scheduler.startup_wait(Job(name="odds", stages=["odds"], interval=60.0))


def test_opening_the_app_does_not_spend_a_credit(temp_env):
    """Launching is not a request for the lines.

    Every job fired the moment the scheduler started, so opening the app spent
    three credits before the window had drawn -- and opening it four times in
    an evening spent twelve on a line that had not moved.
    """
    since_slot = (now() - Scheduler.last_odds_run()).total_seconds()

    # Fetched since today's slot: wait for tomorrow's, however many times the
    # app is opened in between.
    assert _startup(since_slot / 2) == pytest.approx(
        (Scheduler.next_odds_run() - now()).total_seconds(), abs=30)

    # Today's slot came and went with no fetch, or there has never been one:
    # take it now.
    assert _startup(since_slot + 3600) < 60
    assert _startup(None) < 60


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
