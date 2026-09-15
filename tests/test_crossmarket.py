"""Cross-venue edges: the sportsbook consensus aimed at a thinner market."""

from nflpicker.market.consensus import Consensus
from nflpicker.picks.crossmarket import MIN_BOOKS, MIN_DEPTH, evaluate_game

GAME = {"game_id": "g1", "home": "KC", "away": "DEN"}


def _consensus(prob=0.70, n_books=6):
    return Consensus(game_id="g1", captured_at="t", home_win_prob=prob, n_books=n_books)


def _depth(value=2000.0):
    return {"KC": value, "DEN": value}


def test_an_underpriced_side_is_reported():
    """Books say 70%, the venue is selling it at 58% — buy it there."""
    edges = evaluate_game(GAME, _consensus(0.70), 0.58, depth=_depth())
    assert len(edges) == 1
    edge = edges[0]
    assert edge.team == "KC"
    assert abs(edge.gap - 0.12) < 1e-9
    assert edge.expected_value > 0
    assert 0 < edge.kelly <= 0.05


def test_the_other_side_is_taken_when_the_venue_is_too_high():
    edges = evaluate_game(GAME, _consensus(0.70), 0.85, depth=_depth())
    assert len(edges) == 1
    assert edges[0].team == "DEN"       # venue prices DEN at 15%, books say 30%


def test_agreement_produces_nothing():
    assert evaluate_game(GAME, _consensus(0.70), 0.70, depth=_depth()) == []
    assert evaluate_game(GAME, _consensus(0.70), 0.69, depth=_depth()) == []


def test_an_untradeable_gap_is_not_an_edge():
    """The most important guard: a large disagreement with no book behind it
    cannot be acted on, however attractive the percentage looks."""
    edges = evaluate_game(GAME, _consensus(0.70), 0.55, depth={"KC": MIN_DEPTH - 1})
    assert edges == []


def test_stake_never_exceeds_available_depth():
    edges = evaluate_game(GAME, _consensus(0.70), 0.50,
                          depth={"KC": 300.0, "DEN": 300.0}, bankroll=1_000_000)
    assert edges and edges[0].max_stake <= 300.0


def test_a_thin_consensus_is_not_treated_as_fair_value():
    """One book is not a consensus, and calling it one would turn a single
    book's stale number into 'truth' for every comparison."""
    assert evaluate_game(GAME, _consensus(0.70, n_books=MIN_BOOKS - 1), 0.50,
                         depth=_depth()) == []


def test_an_enormous_gap_is_flagged_rather_than_recommended():
    edges = evaluate_game(GAME, _consensus(0.80), 0.40, depth=_depth())
    assert edges and edges[0].confidence == "suspect"


def test_fees_are_subtracted_from_expected_value():
    with_fee = evaluate_game(GAME, _consensus(0.70), 0.62, depth=_depth(), fee=0.05)
    without = evaluate_game(GAME, _consensus(0.70), 0.62, depth=_depth(), fee=0.0)
    assert without[0].expected_value > with_fee[0].expected_value


def test_a_gap_that_only_exists_before_fees_is_rejected():
    edges = evaluate_game(GAME, _consensus(0.60), 0.565, depth=_depth(), fee=0.10)
    assert edges == []


def test_missing_inputs_are_handled():
    assert evaluate_game(GAME, None, 0.5, depth=_depth()) == []
    assert evaluate_game(GAME, _consensus(), None, depth=_depth()) == []
    assert evaluate_game(GAME, Consensus(game_id="g1", captured_at="t",
                                         home_win_prob=None, n_books=6),
                         0.5, depth=_depth()) == []
