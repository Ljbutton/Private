"""Depth charts, historical injury reports, and snap-share weighting.

Two things here have already gone wrong once and are pinned as a result: an
upstream schema change that returned nothing while looking like "no data", and
a snap-share lookup that missed for exactly the players it existed to weigh.
"""

import pandas as pd

from nflpicker.availability import (
    DEFAULT_SNAP_SHARE,
    SnapShares,
    depth_chart_backups,
    historical_index,
    normalize_name,
)
from nflpicker.sources.nflverse import normalise_depth_charts

LEGACY = pd.DataFrame([
    {"season": 2024, "week": 1, "club_code": "KC", "position": "QB",
     "depth_team": 1, "full_name": "Patrick Mahomes"},
    {"season": 2024, "week": 1, "club_code": "KC", "position": "QB",
     "depth_team": 2, "full_name": "Carson Wentz"},
])

MODERN = pd.DataFrame([
    {"dt": "2026-03-14T07:32:09Z", "team": "KC", "player_name": "Patrick Mahomes",
     "pos_abb": "QB", "pos_rank": 1},
    {"dt": "2026-03-14T07:32:09Z", "team": "KC", "player_name": "Justin Fields",
     "pos_abb": "QB", "pos_rank": 2},
])


def test_both_upstream_layouts_normalise_to_one_shape():
    """The 2025 release renamed every column. Returning an empty frame made a
    schema change look like an absence of data, which hid it entirely."""
    expected = {"season", "week", "team", "position", "depth", "full_name"}
    for frame, season in ((LEGACY, 2024), (MODERN, 2026)):
        out = normalise_depth_charts(frame, season)
        assert expected <= set(out.columns)
        assert len(out) == 2
        assert set(out["team"]) == {"KC"}
        assert sorted(out["depth"]) == [1, 2]


def test_an_unrecognised_layout_yields_nothing_rather_than_raising():
    assert normalise_depth_charts(pd.DataFrame([{"nonsense": 1}]), 2024).empty


def test_backups_come_back_in_depth_order():
    charts = normalise_depth_charts(LEGACY, 2024)
    order = depth_chart_backups(charts)
    assert order[(2024, 1, "KC")] == ["P.MAHOMES", "C.WENTZ"]


# ------------------------------------------------------------- snap shares

SNAPS = pd.DataFrame([
    {"season": 2024, "week": 1, "team": "DAL", "player": "Dak Prescott",
     "offense_pct": 1.0, "defense_pct": 0.0},
    {"season": 2024, "week": 2, "team": "DAL", "player": "Dak Prescott",
     "offense_pct": 0.9, "defense_pct": 0.0},
])


def test_a_players_share_is_readable_for_a_week_he_did_not_play():
    """The case the feature exists for: a player who is *out* has no snap row
    that week, so keying on that week missed every injured starter."""
    shares = SnapShares(SNAPS)
    assert shares.before(2024, 3, "DAL", "D.PRESCOTT") == 0.95
    assert shares.before(2024, 2, "DAL", "D.PRESCOTT") == 1.0


def test_the_current_week_is_excluded():
    """This week's participation would leak the answer: a player hurt in
    warmups shows a share of zero."""
    shares = SnapShares(SNAPS)
    assert shares.before(2024, 1, "DAL", "D.PRESCOTT") is None


def test_an_unknown_player_has_no_share():
    assert SnapShares(SNAPS).before(2024, 3, "DAL", "N.OBODY") is None
    assert SnapShares(None).before(2024, 3, "DAL", "D.PRESCOTT") is None


# --------------------------------------------------------- historical index

def _injuries(rows):
    return pd.DataFrame([{"season": 2024, "game_type": "REG", **r} for r in rows])


def test_a_ruled_out_starter_costs_more_than_a_questionable_one():
    out = historical_index(_injuries([
        {"week": 3, "team": "DAL", "position": "QB",
         "full_name": "Dak Prescott", "report_status": "Out"}]), SNAPS)
    questionable = historical_index(_injuries([
        {"week": 3, "team": "DAL", "position": "QB",
         "full_name": "Dak Prescott", "report_status": "Questionable"}]), SNAPS)
    assert out[(2024, 3, "DAL")] < questionable[(2024, 3, "DAL")] < 0


def test_snap_share_weights_the_cost():
    """A starter and a fringe player at the same position are not equal, and
    the position constant alone cannot tell them apart."""
    bench = pd.DataFrame([
        {"season": 2024, "week": 1, "team": "DAL", "player": "Deep Reserve",
         "offense_pct": 0.05, "defense_pct": 0.0},
        {"season": 2024, "week": 2, "team": "DAL", "player": "Deep Reserve",
         "offense_pct": 0.05, "defense_pct": 0.0},
    ])
    starter = historical_index(_injuries([
        {"week": 3, "team": "DAL", "position": "QB",
         "full_name": "Dak Prescott", "report_status": "Out"}]), SNAPS)
    reserve = historical_index(_injuries([
        {"week": 3, "team": "DAL", "position": "QB",
         "full_name": "Deep Reserve", "report_status": "Out"}]), bench)
    assert abs(starter[(2024, 3, "DAL")]) > abs(reserve[(2024, 3, "DAL")]) * 5


def test_an_unknown_player_falls_back_to_a_middling_share():
    """Injury reports skew toward players who actually play, so zero would be
    the wrong default."""
    index = historical_index(_injuries([
        {"week": 1, "team": "KC", "position": "WR",
         "full_name": "Brand New", "report_status": "Out"}]), None)
    assert index[(2024, 1, "KC")] < 0
    assert DEFAULT_SNAP_SHARE > 0


def test_healthy_listings_cost_nothing():
    assert historical_index(_injuries([
        {"week": 1, "team": "KC", "position": "WR",
         "full_name": "Fit Player", "report_status": None}]), None) == {}


def test_the_cost_is_capped_and_decays():
    many = _injuries([
        {"week": 1, "team": "KC", "position": "WR",
         "full_name": f"Player {i}", "report_status": "Out"} for i in range(30)])
    from nflpicker.availability import MAX_TEAM_ADJUSTMENT

    assert historical_index(many, None)[(2024, 1, "KC")] >= -MAX_TEAM_ADJUSTMENT


def test_empty_input_is_handled():
    assert historical_index(pd.DataFrame(), None) == {}
    assert historical_index(None, None) == {}


def test_names_join_across_the_two_feeds():
    assert normalize_name("Dak Prescott") == normalize_name("D.Prescott")
