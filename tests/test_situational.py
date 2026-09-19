"""Situational rates from play-by-play.

Rates rather than counts throughout: a count mostly measures how many
possessions a team happened to get, which is not a property of the team.
"""

import pandas as pd

from nflpicker.sources.nflverse import EXPLOSIVE_YARDS, extract_game_team_stats


def _pbp(plays):
    base = {
        "game_id": "g1", "season": 2024, "week": 1,
        "home_team": "KC", "away_team": "DEN",
        "play_type": "pass", "epa": 0.1, "qb_epa": 0.1, "qb_dropback": 1,
        "passer_player_id": "00-1", "passer_player_name": "P.Mahomes",
        "success": 1, "pass": 1, "rush": 0, "wp": 0.5,
        "interception": 0, "fumble": 0, "fumble_lost": 0,
        "punt_attempt": 0, "field_goal_attempt": 0, "kickoff_attempt": 0,
        "extra_point_attempt": 0, "down": 1, "ydstogo": 10,
        "first_down": 0, "yardline_100": 60, "touchdown": 0, "yards_gained": 5,
        "sack": 0, "penalty_yards": None, "penalty_team": None, "series_success": 0,
    }
    return pd.DataFrame([{**base, **p} for p in plays])


def _row(stats, team="KC"):
    return stats[stats["team"] == team].iloc[0]


def test_third_down_rate_counts_only_third_downs():
    stats = extract_game_team_stats(_pbp([
        {"posteam": "KC", "defteam": "DEN", "down": 3, "first_down": 1},
        {"posteam": "KC", "defteam": "DEN", "down": 3, "first_down": 0},
        {"posteam": "KC", "defteam": "DEN", "down": 1, "first_down": 1},  # ignored
        {"posteam": "DEN", "defteam": "KC", "down": 3, "first_down": 0},
    ]))
    assert _row(stats)["third_down_rate"] == 0.5
    assert _row(stats)["third_down_attempts"] == 2


def test_a_defence_is_credited_with_what_it_allowed():
    stats = extract_game_team_stats(_pbp([
        {"posteam": "KC", "defteam": "DEN", "down": 3, "first_down": 1},
        {"posteam": "KC", "defteam": "DEN", "down": 3, "first_down": 1},
        {"posteam": "DEN", "defteam": "KC", "down": 3, "first_down": 0},
    ]))
    # Denver's defence allowed Kansas City's 100%.
    assert _row(stats, "DEN")["def_third_down_rate"] == 1.0
    assert _row(stats, "KC")["def_third_down_rate"] == 0.0


def test_red_zone_is_inside_the_twenty():
    stats = extract_game_team_stats(_pbp([
        {"posteam": "KC", "defteam": "DEN", "yardline_100": 15, "touchdown": 1},
        {"posteam": "KC", "defteam": "DEN", "yardline_100": 18, "touchdown": 0},
        {"posteam": "KC", "defteam": "DEN", "yardline_100": 45, "touchdown": 1},  # not RZ
        {"posteam": "DEN", "defteam": "KC", "yardline_100": 10, "touchdown": 0},
    ]))
    assert _row(stats)["red_zone_td_rate"] == 0.5


def test_explosive_plays_need_the_full_threshold():
    stats = extract_game_team_stats(_pbp([
        {"posteam": "KC", "defteam": "DEN", "yards_gained": EXPLOSIVE_YARDS},
        {"posteam": "KC", "defteam": "DEN", "yards_gained": EXPLOSIVE_YARDS - 1},
        {"posteam": "DEN", "defteam": "KC", "yards_gained": 3},
    ]))
    assert _row(stats)["explosive_rate"] == 0.5


def test_sack_rate_is_per_dropback():
    stats = extract_game_team_stats(_pbp([
        {"posteam": "KC", "defteam": "DEN", "sack": 1},
        {"posteam": "KC", "defteam": "DEN", "sack": 0},
        {"posteam": "KC", "defteam": "DEN", "sack": 0},
        {"posteam": "KC", "defteam": "DEN", "sack": 0},
        {"posteam": "DEN", "defteam": "KC", "sack": 0},
    ]))
    assert _row(stats)["sack_rate"] == 0.25
    assert _row(stats, "DEN")["sack_rate_forced"] == 0.25


def test_penalties_are_charged_to_the_team_that_committed_them():
    stats = extract_game_team_stats(_pbp([
        {"posteam": "KC", "defteam": "DEN", "penalty_team": "DEN", "penalty_yards": 10},
        {"posteam": "KC", "defteam": "DEN", "penalty_team": "KC", "penalty_yards": 5},
        {"posteam": "DEN", "defteam": "KC", "penalty_team": None, "penalty_yards": None},
    ]))
    assert _row(stats, "KC")["penalty_yards"] == 5
    assert _row(stats, "DEN")["penalty_yards"] == 10


def test_no_attempts_yields_no_rate_rather_than_zero():
    """A team that never faced a third down has no conversion rate; calling it
    0% would drag its rolling average down for something that never happened."""
    stats = extract_game_team_stats(_pbp([
        {"posteam": "KC", "defteam": "DEN", "down": 1},
        {"posteam": "DEN", "defteam": "KC", "down": 2},
    ]))
    assert pd.isna(_row(stats)["third_down_rate"])


def test_situational_rates_reach_the_feature_frame():
    from nflpicker.ml.features import SITUATIONAL, build_features

    games = [{
        "game_id": "g1", "season": 2024, "week": 1, "home": "KC", "away": "DEN",
        "kickoff": "2024-09-08T17:00:00+00:00", "home_score": 27, "away_score": 20,
    }, {
        "game_id": "g2", "season": 2024, "week": 2, "home": "KC", "away": "BUF",
        "kickoff": "2024-09-15T17:00:00+00:00", "home_score": 24, "away_score": 21,
    }]
    detail = {"g1": {
        "KC": {"third_down_rate": 0.5, "red_zone_td_rate": 0.6, "explosive_rate": 0.08,
               "def_third_down_rate": 0.3, "def_explosive_rate": 0.05,
               "sack_rate": 0.05, "sack_rate_forced": 0.09, "penalty_yards": 40.0},
        "DEN": {"third_down_rate": 0.3, "red_zone_td_rate": 0.4, "explosive_rate": 0.04,
                "def_third_down_rate": 0.5, "def_explosive_rate": 0.08,
                "sack_rate": 0.09, "sack_rate_forced": 0.05, "penalty_yards": 55.0},
    }}
    frame = build_features(games, team_game_stats=detail)
    week2 = frame[frame["week"] == 2].iloc[0]
    for stem in SITUATIONAL:
        assert not pd.isna(week2[f"{stem}_home"]), stem
    assert abs(week2["third_down_home"] - 0.5) < 1e-9
