import pytest

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


# ------------------------------------------------------- the power rating

def test_point_differential_separates_teams_with_the_same_elo():
    """The complaint a power ranking answers: a 1-1 team that won by 20 and
    lost by 2 is not the same as one that did the reverse, and Elo alone
    barely tells them apart."""
    from nflpicker.ratings.power import build_power_ratings

    r = build_power_ratings(
        {"KC": 5.0, "BUF": 5.0}, week=8,
        records={"KC": (240.0, 150.0), "BUF": (150.0, 240.0)})
    assert r.get("KC").power > r.get("BUF").power
    assert r.get("KC").pythagorean > r.get("BUF").pythagorean


def test_elo_is_quoted_shrunk_not_at_full_strength():
    """Regressing rest-of-season margin on the Elo difference gives a slope
    near 0.5, so quoting Elo at full strength overstates every gap."""
    from nflpicker.ratings.power import ELO_SHRINK, build_power_ratings

    r = build_power_ratings({"KC": 10.0, "BUF": -10.0}, week=4)
    gap = r.get("KC").power - r.get("BUF").power
    assert abs(gap - ELO_SHRINK * 20.0) < 1e-6


def test_one_blowout_does_not_rank_a_team():
    """Pythagorean is faded in by games played. A week-1 win by 40 is not
    evidence of a 17-0 season."""
    from nflpicker.ratings.power import build_power_ratings

    early = build_power_ratings({"KC": 0.0}, week=1, records={"KC": (45.0, 5.0)})
    late = build_power_ratings({"KC": 0.0}, week=10, records={"KC": (450.0, 50.0)})
    assert early.get("KC").power < late.get("KC").power


def test_a_team_with_no_games_yet_is_rated_on_elo_alone():
    from nflpicker.ratings.power import ELO_SHRINK, build_power_ratings

    r = build_power_ratings({"KC": 6.0}, week=1, records={})
    assert r.get("KC").pythagorean is None
    # Recentring shifts it, but nothing from a record it does not have.
    assert r.get("KC").power == pytest.approx(ELO_SHRINK * 6.0 - ELO_SHRINK * 6.0 / 32, abs=0.3)


def test_efficiency_no_longer_moves_the_margin_rating():
    """Measured over 20,007 rest-of-season games, adding net EPA to Elo and
    Pythagorean moved MAE from 10.8350 to 10.8341 while taking most of the
    rating's weight. It stays out of `power` and keeps driving totals."""
    from nflpicker.ratings.efficiency import TeamEfficiency
    from nflpicker.ratings.power import build_power_ratings

    plain = build_power_ratings({"KC": 4.0}, week=10, records={"KC": (250.0, 200.0)})
    with_epa = build_power_ratings(
        {"KC": 4.0},
        {"KC": TeamEfficiency(team="KC", off_epa=0.25, def_epa=-0.25, plays=600)},
        week=10, records={"KC": (250.0, 200.0)})
    assert with_epa.get("KC").power == pytest.approx(plain.get("KC").power, abs=1e-9)
    # ...but it still reaches the totals projection.
    assert with_epa.get("KC").off_rating != plain.get("KC").off_rating
