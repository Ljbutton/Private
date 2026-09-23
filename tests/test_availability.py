"""Injury-adjusted projections.

Injury history is not in the training data, so availability is applied to the
projection rather than learned. These pin the parts that are easy to get wrong:
the name join between two feeds that share no identifier, and the quarterback
cost, which is a measured gap rather than a constant.
"""

import pytest

from nflpicker import availability
from nflpicker.availability import (
    MAX_TEAM_ADJUSTMENT,
    normalize_name,
    status_cost,
    team_adjustment,
)


def test_names_join_across_two_feeds_that_share_no_identifier():
    """Injury reports say 'Patrick Mahomes'; play-by-play says 'P.Mahomes'."""
    assert normalize_name("Patrick Mahomes") == normalize_name("P.Mahomes")
    assert normalize_name("Marvin Harrison Jr.") == normalize_name("M.Harrison")
    assert normalize_name("Amon-Ra St. Brown") == normalize_name("A.Brown")
    assert normalize_name("C.J. Stroud") == normalize_name("C.Stroud")
    assert normalize_name("") == ""
    assert normalize_name(None) == ""


def test_status_severity_is_ordered():
    assert status_cost("Out") == 1.0
    assert status_cost("Injured Reserve") == 1.0
    assert status_cost("Doubtful") > status_cost("Questionable") > 0
    assert status_cost("Active") == 0.0
    assert status_cost(None) == 0.0


def test_a_healthy_team_is_not_adjusted():
    assert team_adjustment("KC", []).adjustment == 0.0
    assert team_adjustment("KC", [{"position": "QB", "status": "Active"}]).adjustment == 0.0


def test_adjustments_are_negative_because_absences_cost_points():
    a = team_adjustment("KC", [{"player": "X", "position": "WR", "status": "Out"}])
    assert a.adjustment < 0


def test_quarterback_cost_uses_the_measured_gap_not_a_constant():
    """Losing an elite starter to a poor backup costs more than losing a weak
    starter to a similar one. A flat position constant gets both wrong.

    The gap is what separates them, and the floor is what stops the bottom of
    that range falling to nothing: a marginal starter is still a starter, and
    the backup he is measured against is rated as though he were an average
    one. So the elite case is charged its full measured gap while the marginal
    case bottoms out at the floor -- which is why this compares them rather
    than asserting a ratio the floor deliberately compresses.
    """
    out = [{"player": "STARTER", "position": "QB", "status": "Out"}]
    depth = {"KC": ["STARTER", "BACKUP"]}

    elite = team_adjustment("KC", out, depth=depth,
                            qb_values={"STARTER": 0.25, "BACKUP": 0.05})
    marginal = team_adjustment("KC", out, depth=depth,
                               qb_values={"STARTER": 0.02, "BACKUP": 0.00})
    assert abs(elite.adjustment) > abs(marginal.adjustment)
    assert abs(elite.adjustment) == pytest.approx(7.0), "the full measured gap"
    assert abs(marginal.adjustment) == pytest.approx(
        availability.STARTER_QB_FLOOR), "the floor, not the 0.7 gap"


def test_losing_a_starter_worse_than_his_backup_is_not_a_penalty():
    """The measured gap can be negative. Treating that as a boost would reward
    a team for an injury on what is usually small-sample noise."""
    a = team_adjustment(
        "KC", [{"player": "STARTER", "position": "QB", "status": "Out"}],
        depth={"KC": ["STARTER", "BACKUP"]},
        qb_values={"STARTER": 0.01, "BACKUP": 0.20},
    )
    assert a.adjustment == 0.0


def test_an_unknown_quarterback_falls_back_to_a_league_average_cost():
    a = team_adjustment("KC", [{"player": "MYSTERY", "position": "QB", "status": "Out"}])
    assert a.adjustment < -1.0
    assert a.qb_change is True


def test_a_questionable_tag_costs_less_than_being_ruled_out():
    depth, values = {"KC": ["S", "B"]}, {"S": 0.25, "B": 0.05}
    out = team_adjustment("KC", [{"player": "S", "position": "QB", "status": "Out"}],
                          depth=depth, qb_values=values)
    quest = team_adjustment("KC", [{"player": "S", "position": "QB", "status": "Questionable"}],
                            depth=depth, qb_values=values)
    assert abs(quest.adjustment) < abs(out.adjustment)
    assert quest.qb_change is False


def test_the_adjustment_is_capped():
    """Injury reports are strategic and often long. An uncapped sum of
    questionable tags would swamp a rating built from a season of evidence."""
    many = [{"player": f"P{i}", "position": "WR", "status": "Out"} for i in range(40)]
    assert team_adjustment("KC", many).adjustment >= -MAX_TEAM_ADJUSTMENT


def test_later_absences_matter_less_than_the_first():
    one = team_adjustment("KC", [{"player": "A", "position": "WR", "status": "Out"}])
    three = team_adjustment("KC", [
        {"player": "A", "position": "WR", "status": "Out"},
        {"player": "B", "position": "WR", "status": "Out"},
        {"player": "C", "position": "WR", "status": "Out"},
    ])
    assert abs(three.adjustment) > abs(one.adjustment)
    assert abs(three.adjustment) < abs(one.adjustment) * 3       # diminishing


def test_the_adjustment_reaches_the_projection():
    """The whole point: a ruled-out starter must move the model's number."""
    import pandas as pd

    from nflpicker.ml.predict import Predictor

    frame = pd.DataFrame([{
        "game_id": "g1", "home": "KC", "away": "DEN", "season": 2025, "week": 1,
        "games_played_home": 10, "games_played_away": 10,
    }])
    predictor = Predictor(bundle=None)
    base = predictor.predict_frame(frame, None)[0].model_margin
    hurt = predictor.predict_frame(frame, None, adjustments={"KC": -6.0})[0].model_margin
    assert abs((base - hurt) - 6.0) < 1e-6


# ------------------------------------------------- losing the starting passer

def _qbs():
    """A starter, a backup who has barely played, and a third stringer.

    The backup's rating is the shape the registry actually produces: shrunk
    toward zero by how little he has played. Zero on this scale is an *average
    starter*, so an unproven backup scores like a competent one.
    """
    values = {"STARTER": 0.051, "BACKUP": 0.014, "THIRD": -0.002}
    depth = {"SEA": ["THIRD", "BACKUP", "STARTER"]}
    return values, depth


def test_the_starter_being_out_is_never_nearly_free():
    """The bug this floor exists for.

    The measured gap to a backup shrunk toward league-average priced a starting
    quarterback being ruled out at about a point and a quarter -- against a
    market that moves several for any starter. The table showed it, and it read
    as the injury having been ignored.
    """
    values, depth = _qbs()
    points, changed = availability.quarterback_cost(
        "SEA", [{"player": "STARTER", "position": "QB", "status": "Out"}],
        values, depth)
    assert points >= availability.STARTER_QB_FLOOR
    assert changed is True

    # The unfloored gap, for contrast: this is what was being charged.
    gap = (values["STARTER"] - values["BACKUP"]) * availability.DROPBACKS_PER_GAME
    assert gap < 2.0, "the measured gap really is this small"


def test_the_floor_follows_who_is_best_not_who_played_last():
    """A quarterback who is out stops being the most recent starter the moment
    his replacement takes a snap -- which is exactly the week this matters. The
    depth list here is ordered that way on purpose."""
    values, depth = _qbs()
    assert depth["SEA"][0] != "STARTER", "he is not first on the list"
    points, _ = availability.quarterback_cost(
        "SEA", [{"player": "STARTER", "position": "QB", "status": "Out"}],
        values, depth)
    assert points >= availability.STARTER_QB_FLOOR


def test_a_backup_being_out_stays_cheap():
    """The floor is for the man they would start, not for anyone at the
    position. A team whose third stringer is hurt has lost nothing."""
    values, depth = _qbs()
    for who in ("BACKUP", "THIRD"):
        points, _ = availability.quarterback_cost(
            "SEA", [{"player": who, "position": "QB", "status": "Out"}],
            values, depth)
        assert points == 0.0, f"{who} out should cost nothing"


def test_an_elite_starter_costs_more_than_the_floor():
    """The floor is a floor, not a flat rate -- the measured gap still says how
    much worse than his replacement a passer actually is."""
    values = {"ELITE": 0.30, "BACKUP": 0.0}
    depth = {"KC": ["ELITE", "BACKUP"]}
    points, _ = availability.quarterback_cost(
        "KC", [{"player": "ELITE", "position": "QB", "status": "Out"}], values, depth)
    assert points > availability.STARTER_QB_FLOOR * 2


def test_a_questionable_starter_is_charged_a_share_of_it():
    values, depth = _qbs()
    out, _ = availability.quarterback_cost(
        "SEA", [{"player": "STARTER", "position": "QB", "status": "Out"}], values, depth)
    doubtful, _ = availability.quarterback_cost(
        "SEA", [{"player": "STARTER", "position": "QB", "status": "Doubtful"}], values, depth)
    questionable, _ = availability.quarterback_cost(
        "SEA", [{"player": "STARTER", "position": "QB", "status": "Questionable"}],
        values, depth)
    assert out > doubtful > questionable > 0
