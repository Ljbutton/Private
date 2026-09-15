"""Injury-adjusted projections.

Injury history is not in the training data, so availability is applied to the
projection rather than learned. These pin the parts that are easy to get wrong:
the name join between two feeds that share no identifier, and the quarterback
cost, which is a measured gap rather than a constant.
"""

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
    """Losing an elite starter to a poor backup costs far more than losing a
    weak starter to a similar one. A flat position constant gets both wrong."""
    out = [{"player": "STARTER", "position": "QB", "status": "Out"}]
    depth = {"KC": ["STARTER", "BACKUP"]}

    elite = team_adjustment("KC", out, depth=depth,
                            qb_values={"STARTER": 0.25, "BACKUP": 0.05})
    marginal = team_adjustment("KC", out, depth=depth,
                               qb_values={"STARTER": 0.02, "BACKUP": 0.00})
    assert abs(elite.adjustment) > abs(marginal.adjustment) * 3


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
