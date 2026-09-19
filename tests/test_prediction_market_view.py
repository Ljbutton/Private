"""The prediction-market panel is a comparison, not a recommendation."""

from nflpicker.market.prediction_markets import (
    NOTABLE_GAP,
    build_comparisons,
    venue_probability,
)
from nflpicker.util import prob_to_american

GAMES = [{"game_id": "g1", "home": "KC", "away": "DEN", "kickoff": "2025-09-21T17:00:00+00:00"}]


def _quote(home_prob):
    return {
        "home_price": prob_to_american(home_prob),
        "away_price": prob_to_american(1 - home_prob),
        "captured_at": "2025-09-20T12:00:00+00:00",
    }


def test_vig_is_removed_from_a_venue_price():
    # A two-sided market priced with a margin must still come back near fair.
    prob = venue_probability(prob_to_american(0.72), prob_to_american(0.32))
    assert 0.66 < prob < 0.72


def test_venues_are_averaged_and_compared_with_the_books():
    rows = build_comparisons(
        GAMES,
        {"g1": {"home_win_prob": 0.60, "n_books": 6}},
        {"polymarket": {"g1": _quote(0.70)}, "kalshi": {"g1": _quote(0.72)}},
        {"g1": {"home_win_prob": 0.58}},
    )
    assert len(rows) == 1
    row = rows[0]
    assert abs(row.venue_prob - 0.71) < 0.01
    assert abs(row.gap - 0.11) < 0.01
    assert row.notable and row.leans == "KC"
    assert row.model_prob == 0.58 and row.n_books == 6


def test_the_lean_points_at_the_away_team_when_venues_are_lower():
    rows = build_comparisons(
        GAMES, {"g1": {"home_win_prob": 0.70, "n_books": 6}},
        {"polymarket": {"g1": _quote(0.55)}}, {},
    )
    assert rows[0].leans == "DEN"


def test_small_disagreements_are_not_flagged():
    rows = build_comparisons(
        GAMES, {"g1": {"home_win_prob": 0.60, "n_books": 6}},
        {"polymarket": {"g1": _quote(0.60 + NOTABLE_GAP / 2)}}, {},
    )
    assert not rows[0].notable and rows[0].leans is None


def test_a_game_no_venue_priced_is_absent():
    assert build_comparisons(GAMES, {"g1": {"home_win_prob": 0.6}}, {}, {}) == []


def test_one_venue_being_down_does_not_drop_the_row():
    rows = build_comparisons(
        GAMES, {"g1": {"home_win_prob": 0.60, "n_books": 6}},
        {"polymarket": {"g1": _quote(0.70)}, "kalshi": {}}, {},
    )
    assert len(rows) == 1 and len(rows[0].venues) == 1


def test_rows_are_ordered_by_disagreement():
    games = GAMES + [{"game_id": "g2", "home": "SF", "away": "SEA"}]
    rows = build_comparisons(
        games,
        {"g1": {"home_win_prob": 0.60, "n_books": 6},
         "g2": {"home_win_prob": 0.60, "n_books": 6}},
        {"polymarket": {"g1": _quote(0.61), "g2": _quote(0.80)}}, {},
    )
    assert [r.game_id for r in rows] == ["g2", "g1"]


def test_the_payload_carries_no_betting_instruction():
    rows = build_comparisons(
        GAMES, {"g1": {"home_win_prob": 0.60, "n_books": 6}},
        {"polymarket": {"g1": _quote(0.75)}}, {},
    )
    payload = rows[0].to_dict()
    assert not {"expected_value", "kelly", "stake", "max_stake", "confidence"} & set(payload)
