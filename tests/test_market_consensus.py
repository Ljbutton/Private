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
