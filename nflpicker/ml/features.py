"""Leak-free feature construction.

The whole file obeys one rule: every feature for a game is computed from
information available *strictly before* that game's kickoff.  That is enforced
structurally — a single forward pass over chronologically sorted games, where
each game's row is emitted from the running state and only then does the game's
own result update that state.  No lookahead is possible, so no shuffle-based
validation can quietly lie to us.

Sign conventions used everywhere downstream:
  * ``spread_home``  book-style home line; -3.5 means the home team lays 3.5.
  * ``margin_home``  home points minus away points; positive means home won.
  * home covers iff ``margin_home + spread_home > 0``.
"""

from __future__ import annotations

from collections import defaultdict, deque

import numpy as np
import pandas as pd

from ..ratings.elo import EloConfig, EloRatings
from ..teams import TEAMS, distance_miles, same_division, try_resolve
from ..util import to_utc

ROLL_WINDOW = 8

NUMERIC_FEATURES = [
    "elo_diff", "elo_home", "elo_away",
    "rest_home", "rest_away", "rest_diff",
    "short_week_home", "short_week_away", "bye_home", "bye_away",
    "travel_miles", "tz_shift",
    "div_game", "week", "neutral_site",
    "indoor", "turf", "temp", "wind",
    "roll_margin_home", "roll_margin_away", "roll_margin_diff",
    "roll_pf_home", "roll_pa_home", "roll_pf_away", "roll_pa_away",
    "games_played_home", "games_played_away",
    "off_epa_home", "def_epa_home", "off_epa_away", "def_epa_away",
    "epa_net_diff",
]

MARKET_FEATURES = ["spread_home", "market_total"]

TARGETS = ["margin_home", "total_points", "home_win"]

# Longitude-based time zone proxy; a real tz database is overkill for the one
# thing this feeds (west-coast teams playing 1pm Eastern kickoffs).
def _tz_offset(lon: float) -> float:
    return round(lon / 15.0)


def _f(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return np.nan
    return np.nan if out != out else out


def _roll_mean(values: deque) -> float:
    return float(np.mean(values)) if values else np.nan


def build_features(
    games: list[dict] | pd.DataFrame,
    *,
    elo_config: EloConfig | None = None,
    epa_by_game: dict[str, dict] | None = None,
) -> pd.DataFrame:
    """Return one feature row per game, in chronological order.

    ``games`` needs at minimum: game_id, season, week, home, away, kickoff.
    Completed games additionally carry home_score/away_score and get targets;
    upcoming games get the same features with NaN targets, which is exactly what
    inference needs.

    ``epa_by_game`` optionally maps game_id -> {team: {off_epa, def_epa}} so
    rolling EPA can be maintained without re-reading play-by-play here.
    """
    rows = games.to_dict("records") if isinstance(games, pd.DataFrame) else list(games)
    rows = [r for r in rows if r.get("home") and r.get("away")]
    rows.sort(key=lambda r: (int(r.get("season") or 0), int(r.get("week") or 0),
                             str(r.get("kickoff") or "")))

    elo = EloRatings(config=elo_config or EloConfig())
    pf: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLL_WINDOW))
    pa: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLL_WINDOW))
    margins: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLL_WINDOW))
    off_epa: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLL_WINDOW))
    def_epa: dict[str, deque] = defaultdict(lambda: deque(maxlen=ROLL_WINDOW))
    played: dict[str, int] = defaultdict(int)
    last_kickoff: dict[str, object] = {}

    out: list[dict] = []
    for game in rows:
        home = try_resolve(game.get("home"))
        away = try_resolve(game.get("away"))
        if not home or not away:
            continue
        season = int(game.get("season") or 0)
        week = int(game.get("week") or 0)
        elo.start_season(season)

        kickoff = to_utc(game.get("kickoff"))
        rest_home = _rest_days(last_kickoff.get(home), kickoff, game.get("home_rest"))
        rest_away = _rest_days(last_kickoff.get(away), kickoff, game.get("away_rest"))
        neutral = bool(game.get("neutral_site"))

        pre = elo.pregame(
            home, away, neutral_site=neutral,
            home_rest=rest_home, away_rest=rest_away,
        )

        roof = str(game.get("roof") or TEAMS[home].roof or "").lower()
        surface = str(game.get("surface") or "").lower()
        row = {
            "game_id": game.get("game_id"),
            "season": season,
            "week": week,
            "kickoff": kickoff.isoformat() if kickoff else None,
            "home": home,
            "away": away,
            # ---- power
            "elo_diff": pre["elo_diff"],
            "elo_home": pre["home_elo"],
            "elo_away": pre["away_elo"],
            # ---- situation
            "rest_home": rest_home,
            "rest_away": rest_away,
            "rest_diff": (rest_home - rest_away)
            if (rest_home is not None and rest_away is not None) else np.nan,
            "short_week_home": float(rest_home is not None and rest_home <= 4),
            "short_week_away": float(rest_away is not None and rest_away <= 4),
            "bye_home": float(rest_home is not None and rest_home >= 10),
            "bye_away": float(rest_away is not None and rest_away >= 10),
            "travel_miles": 0.0 if neutral else distance_miles(away, home),
            "tz_shift": 0.0 if neutral else abs(
                _tz_offset(TEAMS[away].lon) - _tz_offset(TEAMS[home].lon)
            ),
            "div_game": float(game.get("div_game") if game.get("div_game") is not None
                              else same_division(home, away)),
            "neutral_site": float(neutral),
            # ---- environment
            "indoor": float(roof in {"dome", "closed", "retractable"}),
            "turf": float("turf" in surface or "grass" not in surface and surface != ""),
            "temp": _f(game.get("temp")),
            "wind": _f(game.get("wind")),
            # ---- form
            "roll_margin_home": _roll_mean(margins[home]),
            "roll_margin_away": _roll_mean(margins[away]),
            "roll_pf_home": _roll_mean(pf[home]),
            "roll_pa_home": _roll_mean(pa[home]),
            "roll_pf_away": _roll_mean(pf[away]),
            "roll_pa_away": _roll_mean(pa[away]),
            "games_played_home": float(played[home]),
            "games_played_away": float(played[away]),
            # ---- efficiency
            "off_epa_home": _roll_mean(off_epa[home]),
            "def_epa_home": _roll_mean(def_epa[home]),
            "off_epa_away": _roll_mean(off_epa[away]),
            "def_epa_away": _roll_mean(def_epa[away]),
            # ---- market
            "spread_home": _market_spread(game),
            "market_total": _f(game.get("total_line") if game.get("total_line") is not None
                               else game.get("market_total")),
        }
        row["roll_margin_diff"] = (
            row["roll_margin_home"] - row["roll_margin_away"]
            if not (np.isnan(row["roll_margin_home"]) or np.isnan(row["roll_margin_away"]))
            else np.nan
        )
        net_home = row["off_epa_home"] - row["def_epa_home"]
        net_away = row["off_epa_away"] - row["def_epa_away"]
        row["epa_net_diff"] = net_home - net_away

        # ---- targets (only for completed games)
        home_score, away_score = game.get("home_score"), game.get("away_score")
        has_result = home_score is not None and away_score is not None
        if has_result:
            hs, as_ = float(home_score), float(away_score)
            row["margin_home"] = hs - as_
            row["total_points"] = hs + as_
            row["home_win"] = 1.0 if hs > as_ else (0.0 if hs < as_ else 0.5)
        else:
            row["margin_home"] = np.nan
            row["total_points"] = np.nan
            row["home_win"] = np.nan

        out.append(row)

        # ---- state update happens only AFTER the row is emitted
        if kickoff:
            last_kickoff[home] = kickoff
            last_kickoff[away] = kickoff
        if has_result:
            hs, as_ = float(home_score), float(away_score)
            elo.update(
                home, away, hs, as_, neutral_site=neutral,
                home_rest=rest_home, away_rest=rest_away,
                playoff=str(game.get("season_type", "REG")).upper() == "POST",
                game_id=game.get("game_id"),
                kickoff=kickoff.isoformat() if kickoff else None,
            )
            pf[home].append(hs)
            pa[home].append(as_)
            margins[home].append(hs - as_)
            pf[away].append(as_)
            pa[away].append(hs)
            margins[away].append(as_ - hs)
            played[home] += 1
            played[away] += 1
            epa = (epa_by_game or {}).get(str(game.get("game_id")))
            if epa:
                for team in (home, away):
                    stats = epa.get(team) or {}
                    if stats.get("off_epa") is not None:
                        off_epa[team].append(float(stats["off_epa"]))
                    if stats.get("def_epa") is not None:
                        def_epa[team].append(float(stats["def_epa"]))

    frame = pd.DataFrame(out)
    if not frame.empty:
        for col in NUMERIC_FEATURES + MARKET_FEATURES:
            if col not in frame.columns:
                frame[col] = np.nan
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def _rest_days(previous, kickoff, provided) -> float | None:
    """Days since the team's last game. Source-provided value wins when present."""
    if provided is not None:
        try:
            value = float(provided)
            if value == value:
                return value
        except (TypeError, ValueError):
            pass
    if previous is None or kickoff is None:
        return 7.0  # season opener: treat as a normal week
    days = (kickoff - previous).total_seconds() / 86400.0
    return round(days, 1) if days > 0 else 7.0


def _market_spread(game: dict) -> float:
    """Normalise the many spread conventions to book-style ``spread_home``.

    nflverse ``spread_line`` is "points the home team is favoured by", the
    opposite sign of a posted line, which is a classic source of silent
    sign-flip bugs — so it is converted in exactly one place: here.
    """
    if game.get("spread_home") is not None:
        return _f(game["spread_home"])
    if game.get("spread_line") is not None:
        value = _f(game["spread_line"])
        return np.nan if value != value else -value
    return np.nan


def epa_by_game_from_pbp(pbp: pd.DataFrame) -> dict[str, dict]:
    """Per-game offensive/defensive EPA for each team, keyed by game_id."""
    if pbp is None or pbp.empty:
        return {}
    df = pbp
    if "play_type" in df.columns:
        df = df[df["play_type"].isin(["pass", "run"])]
    if "epa" in df.columns:
        df = df[df["epa"].notna()]
    if df.empty:
        return {}
    out: dict[str, dict] = {}
    off = df.groupby(["game_id", "posteam"])["epa"].mean()
    deff = df.groupby(["game_id", "defteam"])["epa"].mean()
    for (game_id, team), value in off.items():
        team_abbr = try_resolve(team)
        if not team_abbr:
            continue
        out.setdefault(str(game_id), {}).setdefault(team_abbr, {})["off_epa"] = float(value)
    for (game_id, team), value in deff.items():
        team_abbr = try_resolve(team)
        if not team_abbr:
            continue
        out.setdefault(str(game_id), {}).setdefault(team_abbr, {})["def_epa"] = float(value)
    return out


def feature_columns(include_market: bool = True) -> list[str]:
    return NUMERIC_FEATURES + (MARKET_FEATURES if include_market else [])
