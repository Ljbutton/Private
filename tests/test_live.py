"""Live win probability: the behaviour that makes it usable during a game."""

from nflpicker.live import (
    LiveState,
    describe,
    in_red_zone,
    parse_clock,
    seconds_remaining,
    situation_points,
    win_probability,
)


def test_clock_parsing():
    assert parse_clock("4:05") == 245.0
    assert parse_clock("15:00") == 900.0
    assert parse_clock("38") == 38.0
    assert parse_clock(None) is None
    assert parse_clock("nonsense") is None


def test_seconds_remaining_spans_the_whole_game():
    assert seconds_remaining(1, 900) == 3600.0
    assert seconds_remaining(4, 0) == 0.0
    assert seconds_remaining(3, 245) == 1145.0
    assert seconds_remaining(None, 100) is None
    assert seconds_remaining(5, 300) == 300.0        # overtime


def test_the_pregame_projection_dominates_early_and_fades():
    """A fluke score in the first quarter should barely move the number; the
    same score in the fourth should move it a lot."""
    early = LiveState(1, 3400, home_score=7, away_score=0)
    late = LiveState(4, 200, home_score=7, away_score=0)
    assert win_probability(early, 0.0, "KC") < win_probability(late, 0.0, "KC")


def test_a_big_lead_late_is_close_to_certain():
    assert win_probability(LiveState(4, 60, home_score=31, away_score=10), 0.0, "KC") > 0.98
    assert win_probability(LiveState(4, 60, home_score=10, away_score=31), 0.0, "KC") < 0.02


def test_probability_never_reaches_certainty():
    """Uncertainty is floored, so the model cannot report a certainty the
    scoreboard has not earned."""
    p = win_probability(LiveState(4, 1, home_score=60, away_score=0), 0.0, "KC")
    assert p < 1.0


def test_a_tie_at_the_buzzer_is_a_coin_flip():
    assert win_probability(LiveState(4, 1, home_score=20, away_score=20), 0.0, "KC") == 0.5


def test_possession_matters_more_in_a_close_game():
    with_ball = LiveState(4, 120, possession="KC", home_score=24, away_score=21)
    without = LiveState(4, 120, possession="DEN", home_score=24, away_score=21)
    assert win_probability(with_ball, 0.0, "KC") > win_probability(without, 0.0, "KC")


def test_field_position_beats_a_stale_red_zone_flag():
    """A stale isRedZone is a normal feed artifact. Believing it while the ball
    sits on the offence's own 16 applies a two-point swing the wrong way."""
    stale = LiveState(4, 250, possession="DEN", yard_line=16, red_zone=True,
                      home_score=7, away_score=3)
    assert in_red_zone(stale) is False
    real = LiveState(4, 250, possession="DEN", yard_line=88, red_zone=False,
                     home_score=7, away_score=3)
    assert in_red_zone(real) is True
    # Being genuinely in the red zone hurts the defending home team more.
    assert situation_points(real, "KC") < situation_points(stale, "KC")


def test_red_zone_falls_back_to_the_flag_without_field_position():
    assert in_red_zone(LiveState(red_zone=True)) is True
    assert in_red_zone(LiveState(red_zone=False)) is False


def test_fourth_and_long_is_a_liability_not_an_asset():
    first = LiveState(4, 300, possession="KC", down=1, distance=10, yard_line=50,
                      home_score=20, away_score=20)
    fourth = LiveState(4, 300, possession="KC", down=4, distance=12, yard_line=50,
                       home_score=20, away_score=20)
    assert situation_points(fourth, "KC") < situation_points(first, "KC")


def test_no_possession_information_is_neutral():
    assert situation_points(LiveState(2, 1800, home_score=7, away_score=7), "KC") == 0.0


def test_without_a_clock_it_falls_back_to_the_pregame_number():
    state = LiveState(home_score=0, away_score=0)
    from nflpicker.util import margin_to_win_prob

    assert abs(win_probability(state, 3.0, "KC") - margin_to_win_prob(3.0)) < 1e-9


def test_describe_reads_like_a_scoreboard():
    state = LiveState(3, 1145, possession="KC", down=2, distance=7, yard_line=45)
    assert describe(state, "KC", "DEN") == "Q3 · 2nd & 7 · KC ball"
    assert "OT1" in describe(LiveState(5, 300), "KC", "DEN")
