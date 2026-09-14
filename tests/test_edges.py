"""Edges must appear when the model really is better, and vanish when it is not."""

from types import SimpleNamespace

from nflpicker.market.consensus import Consensus
from nflpicker.picks.edges import MIN_INFORMATION, find_edges


def _consensus(spread=-3.0, total=45.0, prob=0.60):
    return Consensus(
        game_id="g1", captured_at="2025-09-02T00:00:00+00:00",
        spread_home=spread, spread_price_home=-110, spread_price_away=-110,
        total_points=total, total_price_over=-110, total_price_under=-110,
        ml_home=-150, ml_away=130, home_win_prob=prob, n_books=5,
        best_home_spread=(spread, "dk"), best_away_spread=(-spread, "dk"),
        best_over=(total, "dk"), best_under=(total, "dk"),
        best_ml_home=(-150, "dk"), best_ml_away=(130, "dk"),
    )


def _prediction(model_margin, fair_margin, spread_edge, information=1.0, **kw):
    return SimpleNamespace(
        model_margin=model_margin, fair_margin=fair_margin,
        model_total=kw.get("model_total", 45.0), fair_total=kw.get("fair_total", 45.0),
        spread_edge=spread_edge, total_edge=kw.get("total_edge", 0.0),
        home_win_prob=kw.get("home_win_prob", 0.60), information=information,
    )


GAME = {"game_id": "g1", "home": "KC", "away": "DEN"}


def test_a_real_edge_is_reported():
    """Model says home by 7 into a -3 line, and the blend keeps 3 points of it."""
    pred = _prediction(model_margin=7.0, fair_margin=6.0, spread_edge=3.0)
    edges = find_edges(pred, _consensus(), GAME)
    spread = [e for e in edges if e.market == "spread"]
    assert spread, "a three point edge should be actionable"
    assert spread[0].team == "KC"
    assert spread[0].expected_value > 0
    assert 0 < spread[0].kelly <= 0.05


def test_no_edge_when_the_model_agrees_with_the_market():
    pred = _prediction(model_margin=3.1, fair_margin=3.0, spread_edge=0.1)
    assert not [e for e in find_edges(pred, _consensus(), GAME) if e.market == "spread"]


def test_edges_are_suppressed_when_the_model_has_no_information():
    """A cold model disagreeing wildly is ignorance, not opportunity."""
    pred = _prediction(model_margin=20.0, fair_margin=18.0, spread_edge=15.0,
                       information=MIN_INFORMATION - 0.01)
    assert find_edges(pred, _consensus(), GAME) == []


def test_an_implausible_disagreement_is_flagged_rather_than_recommended():
    pred = _prediction(model_margin=25.0, fair_margin=20.0, spread_edge=17.0)
    edges = [e for e in find_edges(pred, _consensus(), GAME) if e.market == "spread"]
    assert edges and edges[0].confidence == "suspect"


def test_the_away_side_is_selected_when_the_edge_is_negative():
    pred = _prediction(model_margin=-4.0, fair_margin=-3.0, spread_edge=-3.0,
                       home_win_prob=0.40)
    edges = [e for e in find_edges(pred, _consensus(), GAME) if e.market == "spread"]
    assert edges and edges[0].team == "DEN"


def test_total_edges_pick_the_right_side():
    over = _prediction(0, 0, 0, model_total=52.0, fair_total=51.0, total_edge=6.0)
    picks = [e for e in find_edges(over, _consensus(total=45.0), GAME) if e.market == "total"]
    assert picks and picks[0].selection.startswith("Over")

    under = _prediction(0, 0, 0, model_total=38.0, fair_total=39.0, total_edge=-6.0)
    picks = [e for e in find_edges(under, _consensus(total=45.0), GAME) if e.market == "total"]
    assert picks and picks[0].selection.startswith("Under")


def test_nothing_is_recommended_without_a_market():
    pred = _prediction(7.0, 6.0, 3.0)
    assert find_edges(pred, None, GAME) == []
