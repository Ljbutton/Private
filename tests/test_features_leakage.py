"""The leak-free guarantee is the single most important property in the model.

If a game's features can see its own result, every accuracy number the app
reports becomes fiction. These tests attack that property directly.
"""

import numpy as np
import pandas as pd

from nflpicker.ml.features import build_features
from nflpicker.sources import demo


def _frame(through_week=18, season=2025):
    return build_features(demo.generate_season(season, through_week=through_week)["games"])


def test_first_game_has_no_prior_form():
    frame = _frame()
    opener = frame[frame["week"] == 1]
    assert opener["roll_margin_home"].isna().all()
    assert opener["games_played_home"].eq(0).all()
    assert opener["elo_home"].nunique() == 1        # everyone starts equal


def test_changing_a_result_cannot_change_that_game_s_own_features():
    """The decisive check: perturb one game's score and confirm its own feature
    row is untouched, while later games for those teams do change."""
    games = demo.generate_season(2025, through_week=18)["games"]
    games = sorted(games, key=lambda g: (g["week"], g["kickoff"]))
    base = build_features(games).set_index("game_id")

    target = games[40]
    mutated = [dict(g) for g in games]
    mutated[40] = {**target, "home_score": 99, "away_score": 0}
    after = build_features(mutated).set_index("game_id")

    feature_cols = [c for c in base.columns if c not in
                    ("margin_home", "total_points", "home_win", "home", "away", "kickoff")]
    row_before = base.loc[target["game_id"], feature_cols]
    row_after = after.loc[target["game_id"], feature_cols]
    pd.testing.assert_series_equal(row_before, row_after, check_names=False)

    # ...and a later game involving the same team *must* reflect the change,
    # otherwise the rolling state is not updating at all.
    later = [g for g in games if g["week"] > target["week"]
             and target["home"] in (g["home"], g["away"])]
    assert later, "expected a later game for this team"
    gid = later[0]["game_id"]
    assert not np.allclose(
        base.loc[gid, ["elo_home", "elo_away"]].to_numpy(dtype=float),
        after.loc[gid, ["elo_home", "elo_away"]].to_numpy(dtype=float),
    )


def test_upcoming_games_get_features_but_no_targets():
    frame = _frame(through_week=6)
    upcoming = frame[frame["week"] > 6]
    assert len(upcoming) > 0
    assert upcoming["margin_home"].isna().all()
    assert upcoming["elo_diff"].notna().all()       # still predictable


def test_rows_come_back_in_chronological_order():
    frame = _frame()
    keys = list(zip(frame["season"], frame["week"], strict=True))
    assert keys == sorted(keys)


def test_market_spread_sign_conversion():
    """nflverse ``spread_line`` is 'home favoured by'; ours is the posted line.
    Getting this backwards is silent and ruinous, so it is pinned here."""
    rows = [{
        "game_id": "x", "season": 2024, "week": 1, "home": "KC", "away": "DEN",
        "kickoff": "2024-09-08T17:00:00+00:00",
        "home_score": 27, "away_score": 20, "spread_line": 7.0,
    }]
    frame = build_features(rows)
    assert frame.loc[0, "spread_home"] == -7.0          # home favourite lays points
    assert frame.loc[0, "margin_home"] == 7.0
    # Home covers iff margin + spread_home > 0; 7 + (-7) = 0 is a push.
    assert frame.loc[0, "margin_home"] + frame.loc[0, "spread_home"] == 0
