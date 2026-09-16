"""What an Odds API poll actually costs, and how often it is allowed to happen.

Both halves were wrong in the same direction -- towards spending the month's
credits early -- and neither was visible from inside the app, because the only
number it showed was its own undercount.
"""

import datetime as dt

import pytest

from nflpicker import db
from nflpicker.scheduler import Scheduler
from nflpicker.sources.odds_api import OddsApiSource


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


def test_a_distant_unpriced_game_is_not_an_opening_line(temp_env, monkeypatch):
    """The opening-line exception bypasses the budget-aware interval.

    Asked about the whole season it is true from week 1 until December, because
    books do not price week 15 in September -- so the exception was permanent
    and odds polled at the floor for months.
    """
    now = dt.datetime.now(dt.timezone.utc)
    db.execute(
        "INSERT INTO games(game_id, season, week, kickoff, home, away, status, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("far-off", 2026, 15, (now + dt.timedelta(days=90)).isoformat(),
         "KC", "BUF", "scheduled", now.isoformat()),
    )
    assert Scheduler._awaiting_opening_lines() is False

    db.execute(
        "INSERT INTO games(game_id, season, week, kickoff, home, away, status, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("this-week", 2026, 3, (now + dt.timedelta(days=3)).isoformat(),
         "SF", "SEA", "scheduled", now.isoformat()),
    )
    assert Scheduler._awaiting_opening_lines() is True
