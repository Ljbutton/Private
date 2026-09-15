"""Kalshi market parsing, pinned against the documented response shape."""

from nflpicker.sources.kalshi import mid_price, normalise, team_from_market

EVENT = {
    "event_ticker": "KXNFLGAME-25SEP21DENKC",
    "close_time": "2025-09-21T17:00:00Z",
    "markets": [
        {"ticker": "KXNFLGAME-25SEP21DENKC-KC", "yes_sub_title": "Kansas City Chiefs",
         "yes_bid": 70, "yes_ask": 74, "last_price": 60, "volume": 5000, "status": "open"},
        {"ticker": "KXNFLGAME-25SEP21DENKC-DEN", "yes_sub_title": "Denver Broncos",
         "yes_bid": 26, "yes_ask": 30, "last_price": 40, "volume": 4000, "status": "open"},
    ],
}


def test_prices_come_from_the_book_not_the_last_trade():
    """A thin market's last trade can be hours stale and far from anything you
    could transact at, so the bid/ask mid is used whenever both sides exist."""
    assert abs(mid_price(EVENT["markets"][0]) - 0.72) < 1e-9     # not 0.60
    # With no book at all, the last trade is all there is.
    assert abs(mid_price({"last_price": 55}) - 0.55) < 1e-9
    assert mid_price({}) is None


def test_cents_outside_the_valid_range_are_rejected():
    assert mid_price({"last_price": 0}) is None
    assert mid_price({"last_price": 100}) is None
    assert mid_price({"last_price": "n/a"}) is None


def test_the_paying_team_is_identified_from_subtitle_or_ticker():
    assert team_from_market(EVENT["markets"][0]) == "KC"
    assert team_from_market({"ticker": "KXNFLGAME-25SEP21DENKC-DEN"}) == "DEN"
    assert team_from_market({"ticker": "SOMETHING-ELSE"}) is None


def test_an_event_normalises_to_one_two_sided_quote():
    quotes = normalise([EVENT])
    assert len(quotes) == 1
    q = quotes[0]
    assert {q.home, q.away} == {"KC", "DEN"}
    assert abs(q.home_price + q.away_price - 1.0) < 1e-9
    assert q.volume == 9000


def test_events_that_cannot_be_resolved_to_two_teams_are_skipped():
    assert normalise([{"markets": [EVENT["markets"][0]]}]) == []          # one side only
    assert normalise([{"markets": []}]) == []
    assert normalise([]) == []


def test_closed_markets_are_ignored():
    closed = {**EVENT, "markets": [{**m, "status": "closed"} for m in EVENT["markets"]]}
    assert normalise([closed]) == []


def test_quotes_convert_to_a_storable_moneyline_row():
    row = normalise([EVENT])[0].to_quote_row("2025-09-20T12:00:00+00:00")
    assert row["book"] == "kalshi"
    assert row["market"] == "moneyline"
    assert row["home_price"] is not None and row["away_price"] is not None
