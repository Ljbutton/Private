from nflpicker.ratings.elo import ELO_PER_POINT, EloConfig, EloRatings, mov_multiplier, run_elo
from nflpicker.sources import demo


def test_ratings_are_zero_sum_within_a_season():
    elo = EloRatings()
    before = sum(elo.ratings.values())
    elo.update("KC", "DEN", 30, 20)
    assert abs(sum(elo.ratings.values()) - before) < 1e-9


def test_winning_raises_and_losing_lowers():
    elo = EloRatings()
    elo.update("KC", "DEN", 30, 20)
    assert elo.get("KC") > 1505 > elo.get("DEN")


def test_home_field_is_worth_about_two_points():
    elo = EloRatings()
    assert 1.5 < elo.spread("KC", "DEN") < 2.5
    neutral = elo.pregame("KC", "DEN", neutral_site=True)
    assert abs(neutral["margin"]) < 1e-9


def test_blowouts_are_dampened():
    """A 40-point win must not move a rating four times as far as a 10-pointer."""
    small, big = EloRatings(), EloRatings()
    small.update("KC", "DEN", 27, 17)
    big.update("KC", "DEN", 57, 17)
    small_shift = small.get("KC") - 1505
    big_shift = big.get("KC") - 1505
    assert big_shift > small_shift
    assert big_shift < small_shift * 3


def test_mov_multiplier_corrects_for_the_favourite():
    """The same margin counts for less when a big favourite produced it."""
    assert mov_multiplier(10, 200) < mov_multiplier(10, -200)


def test_season_rollover_regresses_toward_the_mean():
    elo = EloRatings(config=EloConfig(regression=0.5))
    elo.start_season(2024)
    elo.ratings["KC"] = 1705
    elo.start_season(2025)
    assert abs(elo.get("KC") - 1605) < 1e-6


def test_upsets_move_ratings_more_than_expected_results():
    favourite = EloRatings()
    favourite.ratings["KC"] = 1700
    upset = EloRatings()
    upset.ratings["KC"] = 1700
    favourite.update("KC", "DEN", 30, 20)
    upset.update("DEN", "KC", 30, 20)
    assert abs(upset.get("KC") - 1700) > abs(favourite.get("KC") - 1700)


def test_elo_recovers_real_strength_from_played_games():
    """Fed a season whose true strengths are known, the ratings must correlate
    with them — otherwise the whole power layer is noise."""
    import statistics

    season = demo.generate_season(2025, through_week=18)
    games = sorted(season["games"], key=lambda g: (g["week"], g["kickoff"]))
    elo = run_elo(games)
    points = elo.as_points()
    truth = season["strengths"]
    xs = [points[t] for t in truth]
    ys = [truth[t] for t in truth]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / len(xs)
    r = cov / (statistics.pstdev(xs) * statistics.pstdev(ys))
    assert r > 0.5


def test_ratings_convert_to_points_on_the_conventional_scale():
    elo = EloRatings()
    elo.ratings["KC"] = 1505 + ELO_PER_POINT * 4     # four points better
    assert abs(elo.as_points()["KC"] - 4.0) < 1e-9
