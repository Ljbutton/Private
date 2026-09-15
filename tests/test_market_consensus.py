from nflpicker.market.consensus import build_consensus, spread_disagreement


def _quotes():
    return [
        # Two snapshots for one book: only the newer must count.
        {"book": "dk", "market": "spread", "captured_at": "2025-09-01T00:00:00+00:00",
         "home_point": -2.5, "away_point": 2.5, "home_price": -110, "away_price": -110},
        {"book": "dk", "market": "spread", "captured_at": "2025-09-02T00:00:00+00:00",
         "home_point": -3.5, "away_point": 3.5, "home_price": -110, "away_price": -110},
        {"book": "fd", "market": "spread", "captured_at": "2025-09-02T00:00:00+00:00",
         "home_point": -3.0, "away_point": 3.0, "home_price": -108, "away_price": -112},
        {"book": "mgm", "market": "spread", "captured_at": "2025-09-02T00:00:00+00:00",
         "home_point": -4.0, "away_point": 4.0, "home_price": -115, "away_price": -105},
        {"book": "dk", "market": "total", "captured_at": "2025-09-02T00:00:00+00:00",
         "home_point": 45.5, "away_point": 45.5, "home_price": -110, "away_price": -110},
        {"book": "fd", "market": "total", "captured_at": "2025-09-02T00:00:00+00:00",
         "home_point": 46.5, "away_point": 46.5, "home_price": -110, "away_price": -110},
        {"book": "dk", "market": "moneyline", "captured_at": "2025-09-02T00:00:00+00:00",
         "home_price": -175, "away_price": 150},
        {"book": "fd", "market": "moneyline", "captured_at": "2025-09-02T00:00:00+00:00",
         "home_price": -180, "away_price": 155},
    ]


def test_only_the_latest_quote_per_book_counts():
    c = build_consensus("g1", _quotes(), "2025-09-02T01:00:00+00:00")
    # Median of the three latest home lines (-3.5, -3.0, -4.0) is -3.5.
    assert c.spread_home == -3.5
    assert c.n_books == 3


def test_best_available_line_is_the_most_generous_not_the_average():
    c = build_consensus("g1", _quotes(), "2025-09-02T01:00:00+00:00")
    # For a home bettor the best spread is the largest number (-3.0 beats -4.0).
    assert c.best_home_spread == (-3.0, "fd")
    # For the away side the best is the biggest cushion (+4.0).
    assert c.best_away_spread == (4.0, "mgm")
    # Best over is the lowest total; best under the highest.
    assert c.best_over == (45.5, "dk")
    assert c.best_under == (46.5, "fd")


def test_best_moneyline_price_is_the_biggest_payout():
    c = build_consensus("g1", _quotes(), "2025-09-02T01:00:00+00:00")
    assert c.best_ml_home == (-175, "dk")     # -175 pays more than -180
    assert c.best_ml_away == (155, "fd")      # +155 pays more than +150


def test_no_vig_probability_is_below_the_raw_implied_price():
    c = build_consensus("g1", _quotes(), "2025-09-02T01:00:00+00:00")
    from nflpicker.util import american_to_prob

    assert c.home_win_prob < american_to_prob(-175)
    assert 0.5 < c.home_win_prob < 0.7


def test_spread_falls_back_to_an_implied_probability_without_moneylines():
    quotes = [q for q in _quotes() if q["market"] == "spread"]
    c = build_consensus("g1", quotes, "2025-09-02T01:00:00+00:00")
    assert c.home_win_prob is not None and c.home_win_prob > 0.5


def test_disagreement_measures_the_spread_between_books():
    assert spread_disagreement(_quotes()) == 1.0     # -3.0 to -4.0
    assert build_consensus("g", [], "t") is None


def test_prediction_markets_are_excluded_from_the_sportsbook_consensus():
    """The consensus is the benchmark a thinner venue is measured against. If
    that venue were averaged into it, the comparison would partly be against
    itself and the benchmark would drift toward the price being judged."""
    quotes = _quotes() + [
        {"book": "polymarket", "market": "moneyline",
         "captured_at": "2025-09-02T00:00:00+00:00",
         "home_price": 100, "away_price": -120},
        {"book": "polymarket", "market": "spread",
         "captured_at": "2025-09-02T00:00:00+00:00",
         "home_point": -10.0, "away_point": 10.0,
         "home_price": -110, "away_price": -110},
    ]
    c = build_consensus("g1", quotes, "2025-09-02T01:00:00+00:00")
    assert "polymarket" not in c.books
    assert c.n_books == 3
    # The outlier -10 line must not drag the median.
    assert c.spread_home == -3.5


def test_a_game_priced_only_by_a_prediction_market_has_no_consensus():
    quotes = [{"book": "polymarket", "market": "moneyline",
               "captured_at": "2025-09-02T00:00:00+00:00",
               "home_price": 100, "away_price": -120}]
    assert build_consensus("g1", quotes, "2025-09-02T01:00:00+00:00") is None


def test_every_prediction_market_adapter_is_registered():
    """The venue registry is what keeps prediction markets out of the
    sportsbook consensus and out of best-available pricing. An adapter that
    forgets to register reintroduces a bug that has already been fixed twice —
    once for Polymarket, once for Kalshi — so this fails the build instead."""
    from nflpicker.sources import kalshi, polymarket
    from nflpicker.venues import PREDICTION_MARKET_VENUES, is_sportsbook

    for module in (polymarket, kalshi):
        assert module.VENUE in PREDICTION_MARKET_VENUES, module.__name__
        assert not is_sportsbook(module.VENUE)

    assert is_sportsbook("draftkings")
    assert is_sportsbook("FanDuel")
    assert not is_sportsbook("")


def test_a_snapshot_never_includes_a_later_quote():
    """A consensus describes the market as of its own timestamp. Folding in a
    later quote would make a replayed opening line show the closing number."""
    quotes = [
        {"book": "dk", "market": "spread", "captured_at": "2025-09-01T00:00:00+00:00",
         "home_point": -2.5, "away_point": 2.5, "home_price": -110, "away_price": -110},
        {"book": "dk", "market": "spread", "captured_at": "2025-09-05T00:00:00+00:00",
         "home_point": -7.0, "away_point": 7.0, "home_price": -110, "away_price": -110},
    ]
    opening = build_consensus("g1", quotes, "2025-09-01T12:00:00+00:00")
    assert opening.spread_home == -2.5      # not the later -7.0
    closing = build_consensus("g1", quotes, "2025-09-06T00:00:00+00:00")
    assert closing.spread_home == -7.0
