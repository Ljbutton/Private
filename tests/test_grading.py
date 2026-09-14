"""Grading and closing-line value, including the sign conventions."""

from nflpicker import db
from nflpicker.backtest.grade import grade_game
from nflpicker.backtest.report import performance_report


def _seed(home_score, away_score, pred_margin, bet_spread, close_spread):
    db.execute(
        "INSERT INTO games(game_id, season, week, season_type, kickoff, home, away, "
        "home_score, away_score, status, updated_at) "
        "VALUES('g1',2025,1,'REG','2025-09-07T17:00:00+00:00','KC','DEN',?,?,'final','x')",
        (home_score, away_score),
    )
    db.execute(
        "INSERT INTO predictions(game_id, captured_at, model_version, margin_home, "
        "total_points, home_win_prob, market_spread, market_total, spread_edge, total_edge) "
        "VALUES('g1','2025-09-01T00:00:00+00:00','t',?,45,0.7,?,45,?,0)",
        (pred_margin, bet_spread, pred_margin + bet_spread),
    )
    for stamp, spread in (("2025-09-01T00:00:00+00:00", bet_spread),
                          ("2025-09-07T16:00:00+00:00", close_spread)):
        db.execute(
            "INSERT INTO consensus(game_id, captured_at, spread_home, total_points, n_books) "
            "VALUES('g1',?,?,45,5)", (stamp, spread))
    return db.query_one("SELECT * FROM games WHERE game_id='g1'")


def test_a_winning_home_cover_is_graded_correctly(temp_env):
    # Model likes home by 7 into a -3 line; home wins by 10, so the bet cashes.
    game = _seed(30, 20, 7.0, -3.0, -4.0)
    row = grade_game(game)
    assert row["ats_pick"] == "home"
    assert row["ats_result"] == "win"
    assert row["su_correct"] == 1


def test_an_exact_landing_on_the_number_is_a_push(temp_env):
    game = _seed(23, 20, 7.0, -3.0, -3.0)
    row = grade_game(game)
    assert row["ats_result"] == "push"


def test_closing_line_value_is_positive_when_we_beat_the_close(temp_env):
    """We took home at -3 and it closed at -4: we got the better number."""
    game = _seed(30, 20, 7.0, -3.0, -4.0)
    row = grade_game(game)
    assert row["clv_spread"] == 1.0


def test_closing_line_value_is_measured_from_the_side_we_took(temp_env):
    """We backed the away team at +3 (home -3) and it closed at +1 (home -1):
    two points better than the closing number, so CLV is positive. The sign is
    relative to our side, not to the home line."""
    game = _seed(10, 30, -7.0, -3.0, -1.0)
    row = grade_game(game)
    assert row["ats_pick"] == "away"
    assert row["clv_spread"] == 2.0


def test_closing_line_value_is_negative_when_the_market_moves_against_us(temp_env):
    # Backed away at +3, it closed at +5 — the number we took was worse.
    game = _seed(10, 30, -7.0, -3.0, -5.0)
    row = grade_game(game)
    assert row["ats_pick"] == "away"
    assert row["clv_spread"] == -2.0


def test_no_pick_is_recorded_when_the_edge_is_too_small(temp_env):
    game = _seed(30, 20, 3.2, -3.0, -3.0)
    row = grade_game(game)
    assert row["ats_pick"] == "none"
    assert row["ats_result"] == "none"


def test_a_single_market_observation_yields_no_clv(temp_env):
    """With one snapshot, open and close are the same row — CLV is undefined,
    and reporting it as exactly zero would be a fabricated measurement."""
    db.execute(
        "INSERT INTO games(game_id, season, week, season_type, kickoff, home, away, "
        "home_score, away_score, status, updated_at) "
        "VALUES('g2',2025,1,'REG','2025-09-07T17:00:00+00:00','KC','DEN',30,20,'final','x')")
    db.execute(
        "INSERT INTO predictions(game_id, captured_at, model_version, margin_home, "
        "total_points, home_win_prob, market_spread, market_total, spread_edge, total_edge) "
        "VALUES('g2','2025-09-01T00:00:00+00:00','t',7,45,0.7,-3,45,4,0)")
    db.execute(
        "INSERT INTO consensus(game_id, captured_at, spread_home, total_points, n_books) "
        "VALUES('g2','2025-09-01T00:00:00+00:00',-3,45,5)")
    game = db.query_one("SELECT * FROM games WHERE game_id='g2'")
    row = grade_game(game)
    assert row["clv_spread"] is None


def test_empty_report_is_explicit_rather_than_zeroed(temp_env):
    report = performance_report()
    assert report["n_games"] == 0
    assert "note" in report
