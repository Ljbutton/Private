"""How the app decides its own ranking.

The order on the Teams page is a blend of five terms, and which of them should
carry the weight depends entirely on how much of the season has happened. These
pin both ends of that.
"""

import pytest


def test_the_ranking_leans_on_the_rating_until_the_season_says_otherwise():
    """One Sunday is not four pieces of evidence.

    Projected wins, the floor of the range and title odds all bank the games
    already won, and a Pythagorean off one game is a single score line -- a
    team that won 33-8 in week one reads as the best offence in football. With
    those carrying 80% of the weight in week two, Miami finished above Denver,
    the Chargers and the Rams on one result. The rating is the term that
    carries what last season established, so early it is most of the answer.
    """
    from nflpicker.api import ranking_scores

    ratings = {
        "MIA": {"power": -1.0, "pythagorean": 0.94},   # won 33-8
        "DEN": {"power": 4.0, "pythagorean": 0.42},    # lost a close one
        "LAR": {"power": 3.2, "pythagorean": 0.45},
        "LAC": {"power": 2.8, "pythagorean": 0.47},
    }
    one_game = {
        "MIA": {"exp_wins": 9.4, "wins_p10": 6, "sb_prob": 0.05,
                "wins_actual": 1, "losses_actual": 0},
        "DEN": {"exp_wins": 9.0, "wins_p10": 6, "sb_prob": 0.07,
                "wins_actual": 0, "losses_actual": 1},
        "LAR": {"exp_wins": 8.9, "wins_p10": 6, "sb_prob": 0.06,
                "wins_actual": 0, "losses_actual": 1},
        "LAC": {"exp_wins": 8.8, "wins_p10": 6, "sb_prob": 0.06,
                "wins_actual": 0, "losses_actual": 1},
    }
    scores = ranking_scores(ratings, one_game)
    order = sorted(scores, key=lambda t: -scores[t])
    assert order.index("MIA") == 3, "one result does not beat a season of evidence"

    # Ten games in, those results are evidence and should carry.
    ten_games = {t: {**v, "wins_actual": 5, "losses_actual": 5}
                 for t, v in one_game.items()}
    later = ranking_scores(ratings, ten_games)
    assert sorted(later, key=lambda t: -later[t])[0] == "MIA"


def test_the_weights_reach_the_late_set_and_stop():
    from nflpicker.api import RANK_WEIGHTS, RANK_WEIGHTS_EARLY, rank_weights

    assert rank_weights(0) == RANK_WEIGHTS_EARLY
    assert rank_weights(6) == pytest.approx(RANK_WEIGHTS)
    assert rank_weights(17) == pytest.approx(RANK_WEIGHTS)
    for played in (0, 1, 3, 6, 17):
        assert sum(rank_weights(played).values()) == pytest.approx(1.0)
