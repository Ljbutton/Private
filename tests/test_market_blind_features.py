"""The market-blind features: quarterback value, opponent-adjusted efficiency,
special teams, turnover luck and decayed form.

These only exist when per-game play-by-play detail is supplied. Training with
them while inference silently passes nothing would leave the model reading NaN
for its most informative columns, so that wiring is pinned here too.
"""

import numpy as np

from nflpicker.ml.features import build_features
from nflpicker.sources import demo


def _stats(team, opponent, *, off=0.1, deff=0.0, st=0.0, qb="QB1",
           qb_epa=0.2, dropbacks=35, luck=0.0):
    return {
        "team": team, "opponent": opponent, "off_epa": off, "def_epa": deff,
        "st_epa": st, "qb_id": qb, "qb_epa": qb_epa, "qb_dropbacks": dropbacks,
        "turnover_luck": luck,
    }


def _season(through_week=10, season=2025):
    games = demo.generate_season(season, through_week=through_week)["games"]
    return sorted(games, key=lambda g: (g["week"], g["kickoff"]))


def _detail(games, **overrides):
    out = {}
    for g in games:
        if g["status"] != "final":
            continue
        out[g["game_id"]] = {
            g["home"]: _stats(g["home"], g["away"], **overrides.get(g["home"], {})),
            g["away"]: _stats(g["away"], g["home"], **overrides.get(g["away"], {})),
        }
    return out


def test_detail_only_features_are_empty_without_play_by_play():
    frame = build_features(_season())
    for col in ("qb_value_home", "off_epa_adj_home", "st_epa_home",
                "turnover_luck_home"):
        assert frame[col].isna().all(), col


def test_score_derived_features_work_without_play_by_play():
    """Form and Pythagorean come from the scoreline, so they must populate even
    when no play-by-play is available at all."""
    frame = build_features(_season())
    late = frame[frame["week"] >= 6]
    assert late["form_home"].notna().any()
    assert late["pythagorean_home"].notna().any()


def test_features_populate_once_detail_is_supplied():
    """The regression guard: training used these columns, so inference must
    actually fill them rather than passing NaN to the model."""
    games = _season()
    frame = build_features(games, team_game_stats=_detail(games))
    late = frame[frame["week"] >= 6]
    for col in ("qb_value_home", "qb_value_away", "off_epa_adj_home",
                "def_epa_adj_home", "st_epa_home", "form_home", "pythagorean_home"):
        assert late[col].notna().any(), f"{col} never populated"


def test_pythagorean_tracks_points_scored_and_allowed():
    games = _season()
    frame = build_features(games, team_game_stats=_detail(games))
    values = frame["pythagorean_home"].dropna()
    assert len(values) > 0
    assert values.between(0, 1).all()


def test_a_better_quarterback_produces_a_higher_rating():
    games = _season()
    strong, weak = games[0]["home"], games[0]["away"]
    detail = {}
    for g in games:
        if g["status"] != "final":
            continue
        entry = {}
        for team, opponent in ((g["home"], g["away"]), (g["away"], g["home"])):
            if team == strong:
                entry[team] = _stats(team, opponent, qb="STRONG", qb_epa=0.35)
            elif team == weak:
                entry[team] = _stats(team, opponent, qb="WEAK", qb_epa=-0.25)
            else:
                entry[team] = _stats(team, opponent, qb=f"{team}-QB", qb_epa=0.0)
        detail[g["game_id"]] = entry

    frame = build_features(games, team_game_stats=detail)
    def rating(team):
        home = frame[(frame["home"] == team) & frame["qb_value_home"].notna()]
        away = frame[(frame["away"] == team) & frame["qb_value_away"].notna()]
        vals = list(home["qb_value_home"]) + list(away["qb_value_away"])
        return vals[-1] if vals else np.nan

    assert rating(strong) > rating(weak)


def test_a_new_starter_is_flagged_as_a_change():
    """Starter identity is carried on the game row, the way the schedule feed
    supplies it; the flag fires when it differs from the previous start."""
    games = [dict(g) for g in _season(through_week=8)]
    team = games[0]["home"]
    for g in games:
        for side, key in (("home", "home_qb_id"), ("away", "away_qb_id")):
            name = g[side]
            g[key] = (f"{name}-QB2" if name == team and g["week"] >= 5
                      else f"{name}-QB1")

    frame = build_features(games, team_game_stats=_detail(games))
    rows = frame[(frame["home"] == team) | (frame["away"] == team)].sort_values("week")
    changes = [
        (r["week"], r["qb_change_home"] if r["home"] == team else r["qb_change_away"])
        for _, r in rows.iterrows()
    ]
    fired = [w for w, c in changes if c == 1.0]
    assert fired, "a starter swap should register"
    assert all(w >= 5 for w in fired), "the flag must not fire before the swap"
    assert len(fired) == 1, "only the first week of the new starter is a change"


def test_the_change_flag_does_not_fire_on_a_steady_starter():
    games = [dict(g) for g in _season(through_week=8)]
    for g in games:
        g["home_qb_id"] = f"{g['home']}-QB1"
        g["away_qb_id"] = f"{g['away']}-QB1"
    frame = build_features(games, team_game_stats=_detail(games))
    assert (frame["qb_change_home"] == 0).all()
    assert (frame["qb_change_away"] == 0).all()


def test_opponent_adjustment_rewards_production_against_better_defences():
    """Identical raw offensive EPA should rate higher when it came against
    stronger defences — that is the entire point of adjusting."""
    games = _season()
    teams = sorted({g["home"] for g in games} | {g["away"] for g in games})
    tough, soft = teams[0], teams[1]

    def detail_for(defence_epa_by_team):
        out = {}
        for g in games:
            if g["status"] != "final":
                continue
            entry = {}
            for side, other in ((g["home"], g["away"]), (g["away"], g["home"])):
                entry[side] = _stats(
                    side, other, off=0.10,
                    deff=defence_epa_by_team.get(side, 0.0),
                )
            out[g["game_id"]] = entry
        return out

    # One league where everyone's defence is average, one where it is porous.
    strict = build_features(games, team_game_stats=detail_for({}))
    porous = build_features(games, team_game_stats=detail_for(
        {t: 0.25 for t in teams if t not in (tough, soft)}))

    a = strict["off_epa_adj_home"].dropna().mean()
    b = porous["off_epa_adj_home"].dropna().mean()
    assert a > b, "the same offence against weaker defences must rate lower"


def test_state_updates_stay_behind_the_prediction_boundary():
    """A game's own detail must not appear in its own feature row."""
    games = _season()
    detail = _detail(games)
    frame = build_features(games, team_game_stats=detail).set_index("game_id")

    target = next(g for g in games if g["status"] == "final" and g["week"] >= 5)
    bumped = {
        gid: {t: dict(v) for t, v in teams.items()} for gid, teams in detail.items()
    }
    bumped[target["game_id"]][target["home"]]["off_epa"] = 5.0
    bumped[target["game_id"]][target["home"]]["qb_epa"] = 5.0
    after = build_features(games, team_game_stats=bumped).set_index("game_id")

    cols = ["off_epa_adj_home", "qb_value_home", "st_epa_home", "form_home"]
    before_row = frame.loc[target["game_id"], cols]
    after_row = after.loc[target["game_id"], cols]
    for col in cols:
        assert (before_row[col] == after_row[col]) or (
            np.isnan(before_row[col]) and np.isnan(after_row[col])
        ), f"{col} leaked this game's own result"
