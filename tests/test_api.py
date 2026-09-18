"""How the app decides its own ranking.

The order on the Teams page is a blend of five terms, and which of them should
carry the weight depends entirely on how much of the season has happened. These
pin both ends of that.
"""

import pytest


def test_the_order_agrees_with_the_projection_column_beside_it():
    """The table shows PROJ on every row, so the order has to follow it.

    Ranking the Rams 28th next to their own projection of 10.2 wins is not a
    view, it is the page contradicting itself in two adjacent columns. It is
    also the better answer to what the page is asked -- projected wins is the
    rating applied to a real remaining schedule, so it already knows the Rams
    beat the Dolphins and the Chiefs beat the Cardinals.
    """
    from nflpicker.api import ranking_scores

    # Ratings and projections in the shape the screenshot showed them.
    rows = [
        # team    power  pyth   proj  title  record
        ("BUF",   3.0,   0.62,  11.1, 0.12,  1),
        ("BAL",   2.6,   0.45,  10.8, 0.11,  1),
        ("SEA",   1.6,   0.53,  10.7, 0.09,  1),
        ("CIN",   1.8,   0.44,  10.4, 0.08,  1),
        ("KC",    2.4,   0.52,  10.3, 0.10,  1),
        ("LAR",   1.0,   0.40,  10.2, 0.07,  0),
        ("CHI",   0.5,   0.55,   9.7, 0.04,  1),
        ("MIN",   0.6,   0.51,   9.0, 0.05,  1),
        ("NYJ",  -1.2,   0.47,   6.9, 0.01,  1),
        ("ARI",  -1.5,   0.48,   5.7, 0.00,  1),
        ("MIA",  -2.0,   0.30,   4.8, 0.00,  0),
    ]
    ratings = {t: {"power": p, "pythagorean": py} for t, p, py, _, _, _ in rows}
    projections = {
        t: {"exp_wins": e, "wins_p10": e - 3, "sb_prob": sb,
            "wins_actual": w, "losses_actual": 1 - w}
        for t, _, _, e, sb, w in rows
    }
    scores = ranking_scores(ratings, projections)
    order = sorted(scores, key=lambda t: -scores[t])

    # The matchups that were wrong on screen, each of which the projection
    # already had the right way round.
    assert order.index("LAR") < order.index("MIA")
    assert order.index("KC") < order.index("ARI")
    assert order.index("BUF") < order.index("MIN")
    assert order.index("CIN") < order.index("NYJ")
    assert order.index("SEA") < order.index("NYJ")
    # And a 1-0 team projected for nine wins does not lead the league.
    assert order[0] == "BUF" and order.index("CHI") > 4


def test_the_weights_are_one_set_and_sum_to_one():
    from nflpicker.api import RANK_WEIGHTS, rank_weights

    assert rank_weights() == RANK_WEIGHTS
    assert rank_weights(0) == rank_weights(17), "no longer fades with the season"
    assert sum(RANK_WEIGHTS.values()) == pytest.approx(1.0)
    assert max(RANK_WEIGHTS, key=RANK_WEIGHTS.get) == "exp_wins"
