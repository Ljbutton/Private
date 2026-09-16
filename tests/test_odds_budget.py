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


def test_the_recorded_usage_matches_what_was_spent(temp_env):
    source = OddsApiSource.__new__(OddsApiSource)
    source.budget = 100
    source.remaining = None
    db.set_meta("odds_api_usage", {})

    source._record_call(3)
    source._record_call(3)
    assert source.usage()["used"] == 6
    assert source.usage()["remaining_budget"] == 94


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
