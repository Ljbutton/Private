"""Opening lines and the movement test.

The sign conventions here are the whole thing: a posted home line and an
implied margin point in opposite directions, and getting that backwards would
invert every verdict on the page while still looking plausible.
"""

from nflpicker import db
from nflpicker.market.opening import (
    MIN_LEAN,
    MIN_MOVE,
    coverage,
    movement_cases,
    movement_report,
    opening_and_closing,
)


def _game(game_id="g1", home="KC", away="DEN"):
    db.execute(
        "INSERT OR REPLACE INTO games(game_id, season, week, season_type, kickoff, "
        "home, away, status, updated_at) "
        "VALUES(?,2025,1,'REG','2025-09-07T17:00:00+00:00',?,?,'scheduled','x')",
        (game_id, home, away),
    )


def _line(game_id, captured_at, spread):
    db.execute(
        "INSERT OR REPLACE INTO consensus(game_id, captured_at, spread_home, n_books) "
        "VALUES(?,?,?,5)", (game_id, captured_at, spread))


def _prediction(game_id, captured_at, margin):
    db.execute(
        "INSERT OR REPLACE INTO predictions(game_id, captured_at, model_version, "
        "margin_home, total_points, home_win_prob) VALUES(?,?,'t',?,45,0.6)",
        (game_id, captured_at, margin))


def _setup(open_spread, close_spread, model_margin, temp_env):
    _game()
    _line("g1", "2025-09-01T00:00:00+00:00", open_spread)
    _line("g1", "2025-09-07T16:00:00+00:00", close_spread)
    _prediction("g1", "2025-09-01T01:00:00+00:00", model_margin)


def test_opening_and_closing_are_first_and_last(temp_env):
    _setup(-3.0, -6.0, 8.0, temp_env)
    opening, closing = opening_and_closing("g1")
    assert opening["spread_home"] == -3.0
    assert closing["spread_home"] == -6.0


def test_the_line_moving_toward_our_side_counts_as_agreement(temp_env):
    """We like the home team more than a -3 line does; it closes at -6, so the
    market came to us."""
    _setup(-3.0, -6.0, 8.0, temp_env)
    case = movement_cases()[0]
    assert case.lean > 0            # model 8 vs market's implied 3
    assert case.movement > 0        # implied margin rose 3 to 6
    assert case.agreed is True
    assert case.clv == 3.0          # took -3, closed -6


def test_the_line_moving_away_counts_against_us(temp_env):
    _setup(-3.0, -1.0, 8.0, temp_env)
    case = movement_cases()[0]
    assert case.lean > 0 and case.movement < 0
    assert case.agreed is False
    assert case.clv == -2.0


def test_leaning_to_the_away_side_flips_both_signs(temp_env):
    """Liking the away team and seeing the home line shorten is agreement, and
    the closing-line value is measured from the side we actually took."""
    _setup(-7.0, -3.0, 1.0, temp_env)
    case = movement_cases()[0]
    assert case.lean < 0            # model says home by 1, market says 7
    assert case.movement < 0
    assert case.agreed is True
    assert case.clv == 4.0          # took +7, closed +3


def test_small_moves_and_small_leans_are_ignored(temp_env):
    _setup(-3.0, -3.0 - MIN_MOVE / 2, 8.0, temp_env)
    assert movement_cases() == []

    db.execute("DELETE FROM consensus")
    db.execute("DELETE FROM predictions")
    _line("g1", "2025-09-01T00:00:00+00:00", -3.0)
    _line("g1", "2025-09-07T16:00:00+00:00", -6.0)
    _prediction("g1", "2025-09-01T01:00:00+00:00", 3.0 + MIN_LEAN / 2)
    assert movement_cases() == []


def test_a_prediction_made_before_we_saw_a_line_is_not_used(temp_env):
    """The view has to have existed while the opening number stood."""
    _game()
    _line("g1", "2025-09-03T00:00:00+00:00", -3.0)
    _line("g1", "2025-09-07T16:00:00+00:00", -6.0)
    _prediction("g1", "2025-09-01T00:00:00+00:00", 8.0)
    assert movement_cases() == []


def test_a_game_seen_only_once_yields_nothing(temp_env):
    _game()
    _line("g1", "2025-09-01T00:00:00+00:00", -3.0)
    _prediction("g1", "2025-09-01T01:00:00+00:00", 8.0)
    assert movement_cases() == []


def test_the_report_is_explicit_when_there_is_nothing_to_report(temp_env):
    report = movement_report()
    assert report["n"] == 0
    assert "cannot be backfilled" in report["note"]


def test_agreement_is_tested_against_a_coin_flip_not_zero(temp_env):
    """The line always moves one way or the other, so 50% is the baseline and
    a rate above it only counts once the interval clears."""
    for i in range(40):
        gid = f"g{i}"
        _game(gid)
        # 30 of 40 move toward us.
        close = -6.0 if i < 30 else -1.0
        _line(gid, "2025-09-01T00:00:00+00:00", -3.0)
        _line(gid, "2025-09-07T16:00:00+00:00", close)
        _prediction(gid, "2025-09-01T01:00:00+00:00", 8.0)

    report = movement_report()
    assert report["n"] == 40
    assert abs(report["agreement_rate"] - 0.75) < 1e-9
    assert report["beats_coin_flip"] is True
    assert report["avg_clv"] is not None


def test_coverage_counts_what_was_actually_witnessed(temp_env):
    _setup(-3.0, -6.0, 8.0, temp_env)
    cov = coverage()
    assert cov["games_with_any_line"] == 1
    assert cov["games_with_movement"] == 1
    assert cov["snapshots"] == 2
