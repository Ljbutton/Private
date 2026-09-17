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


def test_one_priced_side_is_still_a_quote():
    """A binary contract's other side is what is left of the dollar. Early in
    a week one contract has a book and its opposite has not traded, and
    dropping those lost the whole game rather than half of it."""
    quotes = normalise([{"markets": [EVENT["markets"][0]]}])
    assert len(quotes) == 1
    assert {quotes[0].home, quotes[0].away} == {"KC", "DEN"}
    assert abs(quotes[0].home_price - 0.72) < 1e-9
    assert abs(quotes[0].away_price - 0.28) < 1e-9


def test_events_that_cannot_be_resolved_to_two_teams_are_skipped():
    # One side, and a ticker that names no matchup to find the other in.
    assert normalise([{"markets": [{"ticker": "WHO-KNOWS-KC", "yes_bid": 70,
                                    "yes_ask": 74, "status": "open"}]}]) == []
    assert normalise([{"markets": []}]) == []
    assert normalise([]) == []


def test_both_contracts_carrying_the_same_words_still_pair():
    """The failure this was written for: thirty-two games came back and none
    survived pairing. The titles are per-event on this series, so both
    contracts resolved to the same team and the pair collapsed to one. The
    ticker's trailing segment is per-contract and cannot."""
    same_words = [{**m, "yes_sub_title": "Denver Broncos at Kansas City Chiefs"}
                  for m in EVENT["markets"]]
    quotes = normalise([{**EVENT, "markets": same_words}])
    assert len(quotes) == 1
    assert {quotes[0].home, quotes[0].away} == {"KC", "DEN"}


def test_the_matchup_is_read_out_of_the_ticker():
    from nflpicker.sources.kalshi import matchup_from_market, split_matchup

    assert split_matchup("DETBUF") == ("DET", "BUF")
    assert split_matchup("DENKC") == ("DEN", "KC")
    assert matchup_from_market(
        {"ticker": "KXNFLGAME-26SEP17DETBUF-BUF"}) == ("DET", "BUF")
    # Nonsense, and a split that reads two ways, are both refused rather than
    # guessed: quoting the wrong game is worse than quoting none. LACLV is the
    # real ambiguous one -- LAC+LV is the Chargers at the Raiders, LA+CLV is
    # the Rams at the Browns, and both halves resolve either way.
    assert split_matchup("ZZZZZZ") is None
    assert split_matchup("LACLV") is None
    assert matchup_from_market({"ticker": "KXNFLGAME-BUF"}) is None


def test_closed_markets_are_ignored():
    closed = {**EVENT, "markets": [{**m, "status": "closed"} for m in EVENT["markets"]]}
    assert normalise([closed]) == []


def test_quotes_convert_to_a_storable_moneyline_row():
    row = normalise([EVENT])[0].to_quote_row("2025-09-20T12:00:00+00:00")
    assert row["book"] == "kalshi"
    assert row["market"] == "moneyline"
    assert row["home_price"] is not None and row["away_price"] is not None


def test_every_series_is_tried_and_merged(monkeypatch):
    """A stale series answering with leftovers used to end the search.

    fetch_events returned on the first non-empty result, so if an old ticker
    still had three settled events attached, the live series was never asked
    and the board showed three games out of sixteen -- or none, once the old
    ones aged out.
    """
    from nflpicker.sources.kalshi import NFL_SERIES, KalshiSource

    asked: list[str] = []

    def fake_get(self, path, params):
        asked.append(params["series_ticker"])
        return {"events": [{"event_ticker": f"E-{params['series_ticker']}",
                            "markets": []}]}

    monkeypatch.setattr(KalshiSource, "_get", fake_get)
    events = KalshiSource().fetch_events()
    assert len(events) == len(NFL_SERIES), "every series contributes"
    assert asked == list(NFL_SERIES)


def test_a_series_with_no_open_events_is_retried_unfiltered(monkeypatch):
    """A series whose events are present but not yet "open" answers the
    filtered question with nothing and the unfiltered one with everything."""
    from nflpicker.sources.kalshi import KalshiSource

    seen: list[str | None] = []

    def fake_get(self, path, params):
        seen.append(params.get("status"))
        if params.get("status") == "open":
            return {"events": []}
        return {"events": [{"event_ticker": "E1", "markets": []}]}

    monkeypatch.setattr(KalshiSource, "_get", fake_get)
    assert KalshiSource().fetch_events()
    assert "open" in seen and None in seen


def test_the_probe_reports_what_each_ticker_returned(monkeypatch):
    """The diagnosis the self-test prints: which ticker was asked, how much
    came back, and a sample market ticker to check the shape against."""
    from nflpicker.sources.kalshi import KalshiSource

    def fake_get(self, path, params):
        return {"events": [{"event_ticker": "E1", "markets": [
            {"ticker": "KXNFLGAME-25SEP21DENKC-KC"}]}]}

    monkeypatch.setattr(KalshiSource, "_get", fake_get)
    attempts = KalshiSource().probe()
    assert attempts[0]["events"] == 1
    assert attempts[0]["markets"] == 1
    assert attempts[0]["sample"] == "KXNFLGAME-25SEP21DENKC-KC"
    assert attempts[0]["error"] is None
