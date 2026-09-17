"""Survivor path optimality and pick'em confidence assignment."""

from nflpicker.picks.pickem import build_pickem
from nflpicker.picks.survivor import plan_survivor


def _game(week, home, away, home_prob):
    return {"game_id": f"{week}-{home}-{away}", "home": home, "away": away,
            "home_win_prob": home_prob, "kickoff": f"2025-09-0{week}T17:00:00+00:00"}


def test_survivor_saves_a_team_it_needs_later():
    """The core reason to plan a path rather than take this week's best team.

    KC is the strongest pick in both weeks, but in week 2 it is the *only*
    good option, while week 1 offers BUF at nearly the same probability.
    Spending KC now would strand week 2, so the optimiser must take BUF first.
    """
    games = {
        1: [_game(1, "KC", "CAR", 0.90), _game(1, "BUF", "NYJ", 0.88)],
        2: [_game(2, "KC", "NYG", 0.85), _game(2, "BUF", "PHI", 0.50)],
    }
    plan = plan_survivor(2025, 1, games, through_week=2)
    assert plan.recommendation.team == "BUF"
    assert [e.team for e in plan.path] == ["BUF", "KC"]
    # Path survival is the product of the two chosen win probabilities.
    assert abs(plan.survival_prob - 0.88 * 0.85) < 1e-9


def test_survivor_takes_the_best_team_when_there_is_no_future_conflict():
    games = {
        1: [_game(1, "KC", "CAR", 0.90), _game(1, "BUF", "NYJ", 0.70)],
        2: [_game(2, "SF", "ARI", 0.88), _game(2, "DAL", "WAS", 0.80)],
    }
    plan = plan_survivor(2025, 1, games, through_week=2)
    assert plan.recommendation.team == "KC"


def test_used_teams_are_never_recommended():
    games = {1: [_game(1, "KC", "CAR", 0.95), _game(1, "BUF", "NYJ", 0.60)]}
    plan = plan_survivor(2025, 1, games, used_teams=["KC"], through_week=1)
    assert plan.recommendation.team == "BUF"
    assert all(e.team != "KC" for e in plan.path)


def test_the_underdog_side_is_available_too():
    """Picking the away team is legal: it is the side, not the home team."""
    games = {1: [_game(1, "CAR", "KC", 0.10)]}
    plan = plan_survivor(2025, 1, games, through_week=1)
    assert plan.recommendation.team == "KC"
    assert abs(plan.recommendation.win_prob - 0.90) < 1e-9


def test_deviating_this_week_is_priced_over_the_whole_path():
    games = {
        1: [_game(1, "KC", "CAR", 0.90), _game(1, "BUF", "NYJ", 0.88)],
        2: [_game(2, "KC", "NYG", 0.85), _game(2, "BUF", "PHI", 0.50)],
    }
    plan = plan_survivor(2025, 1, games, through_week=2)
    alt = next(a for a in plan.alternatives if a["team"] == "KC")
    # Taking KC now forces BUF in week 2 at 0.50 — clearly worse over the path,
    # even though KC has the higher single-week probability.
    assert alt["path_survival"] < plan.survival_prob
    assert alt["cost"] > 0


def test_horizon_shortens_rather_than_failing_when_teams_run_out():
    # Three weeks but only two distinct teams ever play: no full path exists,
    # so the planner must shorten rather than give up.
    games = {
        1: [_game(1, "KC", "CAR", 0.9)],
        2: [_game(2, "CAR", "KC", 0.2)],
        3: [_game(3, "KC", "CAR", 0.9)],
    }
    plan = plan_survivor(2025, 1, games, through_week=3)
    assert plan.recommendation is not None
    assert plan.horizon == 2
    assert "shortened" in plan.note.lower()


def test_equal_survival_paths_prefer_surviving_sooner():
    """Both orderings use the same two teams and so share a survival product;
    the safer pick belongs in the week that is actually about to be played."""
    games = {
        1: [_game(1, "KC", "CAR", 0.90), _game(1, "CAR", "KC", 0.10)],
        2: [_game(2, "KC", "NYG", 0.10), _game(2, "CAR", "NYG", 0.90)],
    }
    plan = plan_survivor(2025, 1, games, through_week=2)
    assert plan.recommendation.win_prob == 0.90


def test_no_feasible_path_is_reported_not_crashed():
    games = {1: [_game(1, "KC", "CAR", 0.9)]}
    plan = plan_survivor(2025, 1, games, used_teams=["KC", "CAR"], through_week=1)
    assert plan.recommendation is None
    assert plan.note


# ------------------------------------------------------------------ pick'em

def _entries():
    return [
        {"game_id": "a", "home": "KC", "away": "DEN", "home_win_prob": 0.80,
         "market_home_prob": 0.78},
        {"game_id": "b", "home": "BUF", "away": "NYJ", "home_win_prob": 0.65,
         "market_home_prob": 0.70},
        {"game_id": "c", "home": "CHI", "away": "GB", "home_win_prob": 0.45,
         "market_home_prob": 0.40},
    ]


def test_confidence_points_are_assigned_most_to_the_most_likely():
    board = build_pickem(2025, 1, _entries(), mode="ev")
    assert [p.confidence for p in board.picks] == [3, 2, 1]
    assert board.picks[0].pick == "KC"
    probs = [p.win_prob for p in board.picks]
    assert probs == sorted(probs, reverse=True)
    assert board.max_points == 6


def test_the_underdog_side_is_picked_when_we_disagree_with_the_market():
    board = build_pickem(2025, 1, _entries(), mode="ev")
    chicago = next(p for p in board.picks if p.game_id == "c")
    assert chicago.pick == "GB"                       # home prob 0.45 < 0.5
    assert abs(chicago.win_prob - 0.55) < 1e-9


def test_expected_points_equal_the_probability_weighted_assignment():
    board = build_pickem(2025, 1, _entries(), mode="ev")
    expected = sum(p.win_prob * p.confidence for p in board.picks)
    assert abs(board.expected_points - expected) < 1e-9
    assert abs(board.expected_correct - sum(p.win_prob for p in board.picks)) < 1e-9


def test_ev_mode_never_scores_below_the_field_under_our_own_beliefs():
    """Both boards are scored with the same probabilities, so the assignment
    that maximises expected points cannot lose to the field's."""
    board = build_pickem(2025, 1, _entries(), mode="ev")
    assert board.expected_points >= board.field_expected_points - 1e-9


def test_leverage_mode_trades_expected_points_for_differentiation():
    ev = build_pickem(2025, 1, _entries(), mode="ev")
    lev = build_pickem(2025, 1, _entries(), mode="leverage")
    assert lev.expected_points <= ev.expected_points + 1e-9
    assert [p.pick for p in lev.picks] == [p.pick for p in ev.picks]  # sides unchanged


# ------------------------------------------------- planning the whole run

def _season_games(first_week: int, last_week: int) -> dict:
    """A tidy schedule: sixteen games a week, every team playing once."""
    from nflpicker.teams import ABBRS

    weeks = {}
    for w in range(first_week, last_week + 1):
        # Rotate the pairings each week so no two weeks are identical.
        order = ABBRS[w % len(ABBRS):] + ABBRS[:w % len(ABBRS)]
        weeks[w] = [
            {"game_id": f"{w}-{i}", "home": order[2 * i], "away": order[2 * i + 1],
             "home_win_prob": 0.55 + (i % 5) * 0.05}
            for i in range(len(order) // 2)
        ]
    return weeks


def test_the_plan_runs_to_the_end_of_the_season():
    """Survivor is a scheduling constraint, not a forecast.

    Each team may be spent once, so using one this week costs whichever future
    week wanted it -- and a planner that stops at week six cannot see that
    cost. Burning the team you needed in week 14 is the exact failure these
    pools punish.

    It stopped at 17 until it was pointed out that a plan which stops early
    cannot be extended by its reader, while one that runs a week long can be
    read from the row above. A pool that settles in 17 sets it back.
    """
    from nflpicker.picks.survivor import plan_survivor

    plan = plan_survivor(2026, 2, _season_games(1, 18))
    assert plan.through_week == 18
    assert [e.week for e in plan.path] == list(range(2, 19))
    assert len({e.team for e in plan.path}) == len(plan.path), "a team cannot be spent twice"


def test_an_early_week_still_gets_a_full_path():
    """'Especially through week 7' -- planning from week 1 must reach the end
    of the season too, not just from a convenient starting point."""
    from nflpicker.picks.survivor import plan_survivor

    for start in (1, 3, 7):
        plan = plan_survivor(2026, start, _season_games(1, 18))
        assert plan.recommendation is not None, f"no pick from week {start}"
        assert plan.through_week == 18
        assert [e.week for e in plan.path] == list(range(start, 19))


def test_a_late_start_plans_only_what_is_left():
    from nflpicker.picks.survivor import plan_survivor

    plan = plan_survivor(2026, 16, _season_games(1, 18))
    assert [e.week for e in plan.path] == [16, 17, 18]
