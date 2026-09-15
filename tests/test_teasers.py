"""Teaser backtest: the arithmetic, the sign conventions, and the statistics."""

import pandas as pd

from nflpicker.backtest.teasers import (
    break_even,
    evaluate_window,
    key_number_frequency,
    wilson_interval,
    wong_windows,
)


def _games(rows):
    """rows of (spread_line, result) — nflverse conventions."""
    return pd.DataFrame(
        [{"season": 2020, "spread_line": s, "result": r} for s, r in rows]
    )


def test_break_even_matches_the_closed_form():
    # Both legs must land: p^2 * 1 = (1 - p^2) * 1.1
    assert abs(break_even(2, -110) - (1.1 / 2.1) ** 0.5) < 1e-9
    assert break_even(2, -130) > break_even(2, -110)     # worse price, higher bar
    assert break_even(3, -110) > break_even(2, -110)     # more legs, higher bar
    assert break_even(2, 0) == 1.0


def test_a_ten_cent_price_change_moves_the_bar_about_a_point():
    """This is most of the claimed edge, which is why price is reported."""
    assert 0.010 < break_even(2, -120) - break_even(2, -110) < 0.020


def test_the_home_line_is_the_negation_of_the_nflverse_spread():
    """nflverse spread_line is 'home favoured by'; the posted line is negative.
    Getting this backwards silently inverts every leg in the backtest."""
    # Home favoured by 8, wins by 1. Teased to -2, so the home leg loses.
    result = evaluate_window(_teased(_games([(8.0, 1.0)])), -8.5, -7.5)
    assert result.n == 1 and result.wins == 0
    # Same game from the away side: +8 teased to +14, and a 1-point loss covers.
    result = evaluate_window(_teased(_games([(8.0, 1.0)])), 7.5, 8.5)
    assert result.n == 1 and result.wins == 1


def _teased(games):
    from nflpicker.backtest.teasers import _legs

    return _legs(games)


def test_a_six_point_teaser_moves_the_line_six_points():
    # Home favoured by 8, wins by 3: -8 teased to -2, so a 3-point win covers.
    result = evaluate_window(_teased(_games([(8.0, 3.0)])), -8.5, -7.5)
    assert result.wins == 1

    # Underdog by 2 loses by 5: +2 teased to +8, so a 5-point loss covers.
    result = evaluate_window(_teased(_games([(-2.0, -5.0)])), 1.5, 2.5)
    assert result.wins == 1


def test_landing_exactly_on_the_teased_number_is_a_push():
    # Home -8 teased to -2, wins by exactly 2.
    result = evaluate_window(_teased(_games([(8.0, 2.0)])), -8.5, -7.5)
    assert result.pushes == 1 and result.n == 0


def test_windows_exclude_spreads_outside_them():
    games = _games([(3.0, 10.0), (14.0, 1.0)])
    assert evaluate_window(_teased(games), -8.5, -7.5).n == 0


def test_wilson_interval_brackets_the_estimate_and_narrows_with_data():
    low, high = wilson_interval(75, 100)
    assert low < 0.75 < high
    wide = high - low
    low2, high2 = wilson_interval(750, 1000)
    assert (high2 - low2) < wide
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_significance_requires_the_whole_interval_to_clear_break_even():
    """A point estimate above break-even with an interval straddling it is a
    sample, not an edge — and this is the guard that says so."""
    strong = evaluate_window(
        _teased(_games([(8.0, 10.0)] * 900 + [(8.0, -10.0)] * 100)), -8.5, -7.5)
    assert strong.win_rate == 0.9 and strong.significant

    marginal = evaluate_window(
        _teased(_games([(8.0, 10.0)] * 73 + [(8.0, -10.0)] * 27)), -8.5, -7.5)
    assert marginal.win_rate > marginal.break_even_2leg
    assert not marginal.significant, "73/100 is not distinguishable from 72.4%"


def test_key_numbers_three_and_seven_dominate():
    """The mechanism the whole strategy rests on."""
    games = _games([(0.0, m) for m in [3, 3, 3, 7, 7, 1, 2, 4, 10]])
    freq = {row["margin"]: row["share"] for row in key_number_frequency(games)}
    assert freq[3] > freq[4]
    assert freq[7] > freq[10]


def test_the_report_runs_end_to_end():
    games = _games([(8.0, m) for m in range(-10, 11)] +
                   [(-2.0, m) for m in range(-10, 11)])
    results = wong_windows(games)
    assert len(results) == 3
    assert all(r.n > 0 for r in results)
