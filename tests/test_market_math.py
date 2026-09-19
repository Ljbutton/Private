
from nflpicker import util


def test_american_odds_round_trip():
    for prob in (0.05, 0.25, 0.5, 0.75, 0.95):
        odds = util.prob_to_american(prob)
        assert abs(util.american_to_prob(odds) - prob) < 0.01


def test_devig_removes_the_margin():
    a = util.american_to_prob(-140)
    b = util.american_to_prob(120)
    assert a + b > 1.0                       # the book's margin
    fair = util.devig(a, b)
    assert abs(sum(fair) - 1.0) < 1e-9       # removed
    assert fair[0] > fair[1]                 # favourite still favoured


def test_devig_power_differs_from_naive_normalisation():
    """Power de-vig should not simply rescale; that is the point of using it."""
    a, b = util.american_to_prob(-500), util.american_to_prob(380)
    power = util.devig(a, b, "power")
    naive = util.devig(a, b, "multiplicative")
    assert abs(power[0] - naive[0]) > 1e-4


def test_margin_and_win_probability_are_inverses():
    for margin in (-14, -3, 0, 3, 7, 21):
        prob = util.margin_to_win_prob(margin)
        assert abs(util.win_prob_to_margin(prob) - margin) < 1e-6
    assert util.margin_to_win_prob(0) == 0.5


def test_normal_ppf_matches_known_quantiles():
    assert abs(util.normal_ppf(0.975) - 1.959964) < 1e-4
    assert abs(util.normal_ppf(0.5)) < 1e-9


def test_normal_cdf_vectorises():
    import numpy as np

    out = util.normal_cdf(np.array([-1.0, 0.0, 1.0]))
    assert isinstance(out, np.ndarray)
    assert abs(out[1] - 0.5) < 1e-9


def test_cover_probability_follows_the_sign_convention():
    # spread_home = -3.5 means home lays 3.5. Home covers iff margin > 3.5.
    assert util.cover_prob(7, -3.5) > 0.5
    assert util.cover_prob(1, -3.5) < 0.5
    assert abs(util.cover_prob(3.5, -3.5) - 0.5) < 1e-9


def test_kelly_is_zero_without_an_edge_and_capped_with_one():
    assert util.kelly_fraction(0.50, -110) == 0.0
    assert util.kelly_fraction(0.40, -110) == 0.0
    assert 0 < util.kelly_fraction(0.60, -110) <= 0.05
    assert util.kelly_fraction(0.99, 5000) <= 0.05      # cap holds


def test_expected_value_sign():
    assert util.expected_value(0.55, -110) > 0
    assert util.expected_value(0.50, -110) < 0
    assert abs(util.expected_value(0.5238, -110)) < 0.01   # break-even


def test_labor_day_and_week_one():
    import datetime as dt

    assert util.labor_day(2026) == dt.date(2026, 9, 7)
    assert util.season_week1_thursday(2026) == dt.date(2026, 9, 10)
    assert util.estimate_week(dt.date(2026, 9, 14), 2026) == 1
    assert util.estimate_week(dt.date(2026, 9, 20), 2026) == 2


def test_january_games_belong_to_the_previous_season():
    import datetime as dt

    assert util.current_season(dt.date(2027, 1, 10)) == 2026
    assert util.current_season(dt.date(2026, 10, 1)) == 2026


def test_zero_is_not_a_price():
    """A malformed feed sending 0 must be ignored, not raise. Both converters
    have to agree on that, since callers use them interchangeably."""
    assert util.american_to_prob(0) is None
    assert util.american_to_decimal(0) is None
    assert util.american_to_decimal(None) is None
    assert util.expected_value(0.5, 0) is None
    assert util.kelly_fraction(0.6, 0) == 0.0
