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

# Exponential decay for form ratings. Roughly a ten-game effective window, but
# unlike a flat window it never lets a game from two months ago count exactly as
# much as last Sunday's and then drop out entirely.
FORM_ALPHA = 0.15

# How far season-to-season ratings revert at the break. Rosters and coaching
# turn over enough that last year's efficiency is informative but not carried
# whole; this mirrors the regression Elo already applies.
SEASON_REGRESSION = 0.45

# Shrinkage for a quarterback's rating, in dropbacks. A passer with 40 attempts
# is mostly prior; by ~400 he is mostly himself.
QB_PRIOR_DROPBACKS = 250.0

# Pythagorean exponent for NFL points. 2.37 is the standard fitted value.
PYTHAGOREAN_EXPONENT = 2.37

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
    # ---- market-blind additions ----
    # Quarterback, which is the largest single source of week-to-week variation
    # a power rating misses when a starter changes.
    "qb_value_home", "qb_value_away", "qb_value_diff",
    "qb_experience_home", "qb_experience_away",
    "qb_change_home", "qb_change_away",
    # Efficiency adjusted for the quality of opponents actually faced.
    "off_epa_adj_home", "def_epa_adj_home", "off_epa_adj_away", "def_epa_adj_away",
    "epa_adj_diff",
    # The two components a raw scoring margin hides.
    "st_epa_home", "st_epa_away", "st_epa_diff",
    "turnover_luck_home", "turnover_luck_away", "turnover_luck_diff",
    # Point differential restated as a win expectation, and decayed form.
    "pythagorean_home", "pythagorean_away", "pythagorean_diff",
    "form_home", "form_away", "form_diff",
    # Situational rates. Rates rather than counts, because counts mostly
    # measure how many possessions a team happened to get.
    "third_down_home", "third_down_away", "third_down_diff",
    "def_third_down_home", "def_third_down_away",
    "red_zone_home", "red_zone_away", "red_zone_diff",
    "explosive_home", "explosive_away", "explosive_diff",
    "def_explosive_home", "def_explosive_away",
    "sack_rate_home", "sack_rate_away",
    "sack_forced_home", "sack_forced_away",
    "penalty_yards_home", "penalty_yards_away",
]

# Situational stats tracked per team: feature stem -> key in the per-game stats.
SITUATIONAL = {
    "third_down": "third_down_rate",
    "def_third_down": "def_third_down_rate",
    "red_zone": "red_zone_td_rate",
    "explosive": "explosive_rate",
    "def_explosive": "def_explosive_rate",
    "sack_rate": "sack_rate",
    "sack_forced": "sack_rate_forced",
    "penalty_yards": "penalty_yards",
}

MARKET_FEATURES = ["spread_home", "market_total"]

TARGETS = ["margin_home", "total_points", "home_win"]

# Longitude-based time zone proxy; a real tz database is overkill for the one
# thing this feeds (west-coast teams playing 1pm Eastern kickoffs).
def _tz_offset(lon: float) -> float:
    return round(lon / 15.0)


class _Ewma:
    """Exponentially weighted mean that reports how much data it has seen."""

    __slots__ = ("alpha", "value", "n")

    def __init__(self, alpha: float = FORM_ALPHA) -> None:
        self.alpha = alpha
        self.value: float | None = None
        self.n = 0

    def update(self, observation: float | None) -> None:
        if observation is None or observation != observation:
            return
        observation = float(observation)
        self.value = observation if self.value is None else (
            self.alpha * observation + (1 - self.alpha) * self.value
        )
        self.n += 1

    def regress(self, toward: float = 0.0, fraction: float = SEASON_REGRESSION) -> None:
        if self.value is not None:
            self.value = self.value * (1 - fraction) + toward * fraction

    def get(self, default: float = np.nan) -> float:
        return default if self.value is None else self.value


class _TeamForm:
    """Everything tracked about one team between games."""

    __slots__ = ("off_adj", "def_adj", "st", "turnover_luck", "form",
                 "points_for", "points_against", "last_qb", "situational")

    def __init__(self) -> None:
        self.off_adj = _Ewma()
        self.def_adj = _Ewma()
        self.st = _Ewma()
        self.turnover_luck = _Ewma()
        self.form = _Ewma()
        self.points_for = 0.0
        self.points_against = 0.0
        self.last_qb: str | None = None
        self.situational: dict[str, _Ewma] = {stem: _Ewma() for stem in SITUATIONAL}

    def new_season(self) -> None:
        for meter in (self.off_adj, self.def_adj, self.st, self.turnover_luck, self.form):
            meter.regress()
        for meter in self.situational.values():
            meter.regress()
        self.points_for = 0.0
        self.points_against = 0.0

    def situational_value(self, stem: str) -> float:
        return self.situational[stem].get()

    def pythagorean(self) -> float:
        """Win expectation implied by points scored and allowed.

        Point differential predicts future results better than win-loss record,
        and this puts it on a 0-1 scale the model can compare across teams.
        """
        total = self.points_for + self.points_against
        if total <= 0:
            return np.nan
        pf = self.points_for ** PYTHAGOREAN_EXPONENT
        pa = self.points_against ** PYTHAGOREAN_EXPONENT
        return pf / (pf + pa) if (pf + pa) > 0 else np.nan


class _QbTracker:
    """Rolling value per quarterback, shrunk toward the league mean by volume."""

    def __init__(self) -> None:
        self._epa: dict[str, _Ewma] = {}
        self._dropbacks: dict[str, float] = {}

    def value(self, qb_id: str | None) -> float:
        """Shrunk EPA per dropback. Unknown or barely-seen passers sit near 0,
        which is the league average — the right prior for a debut start."""
        if not qb_id:
            return np.nan
        meter = self._epa.get(qb_id)
        if meter is None or meter.value is None:
            return np.nan
        volume = self._dropbacks.get(qb_id, 0.0)
        weight = volume / (volume + QB_PRIOR_DROPBACKS)
        return meter.value * weight

    def experience(self, qb_id: str | None) -> float:
        if not qb_id:
            return np.nan
        return float(np.log1p(self._dropbacks.get(qb_id, 0.0)))

    def update(self, qb_id: str | None, qb_epa: float | None, dropbacks: float | None) -> None:
        if not qb_id:
            return
        meter = self._epa.setdefault(qb_id, _Ewma())
        meter.update(qb_epa)
        self._dropbacks[qb_id] = self._dropbacks.get(qb_id, 0.0) + float(dropbacks or 0.0)


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
    team_game_stats: dict[str, dict] | None = None,
) -> pd.DataFrame:
    """Return one feature row per game, in chronological order.

    ``games`` needs at minimum: game_id, season, week, home, away, kickoff.
    Completed games additionally carry home_score/away_score and get targets;
    upcoming games get the same features with NaN targets, which is exactly what
    inference needs.

    ``epa_by_game`` optionally maps game_id -> {team: {off_epa, def_epa}} so
    rolling EPA can be maintained without re-reading play-by-play here.

    ``team_game_stats`` maps game_id -> {team: per-game stats} from
    :func:`nflverse.extract_game_team_stats`, and is what powers the
    market-blind features: quarterback value, opponent-adjusted efficiency,
    special teams and turnover luck. Everything still works without it; those
    columns simply stay empty and the booster ignores them.
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
    form: dict[str, _TeamForm] = defaultdict(_TeamForm)
    quarterbacks = _QbTracker()
    season_seen: dict[str, int] = {}

    out: list[dict] = []
    for game in rows:
        home = try_resolve(game.get("home"))
        away = try_resolve(game.get("away"))
        if not home or not away:
            continue
        season = int(game.get("season") or 0)
        week = int(game.get("week") or 0)
        elo.start_season(season)
        # Regress each side's form once per season, the first time we see it.
        for team in (home, away):
            if season and season_seen.get(team) != season:
                if season_seen.get(team) is not None:
                    form[team].new_season()
                season_seen[team] = season

        home_form, away_form = form[home], form[away]
        # Starter identity comes from the schedule feed when it has one, and
        # otherwise from whoever started last.
        #
        # Two subtleties, both deliberate. First, identity must come from a
        # single source: the schedule records the *starter* while play-by-play
        # reports whoever threw the most passes, and those disagree on about one
        # game in ten, so mixing them made the change flag fire on disagreements
        # rather than on actual changes. Second, the recorded starter is a mild
        # lookahead — it is what did happen, not what was announced — but
        # starting quarterbacks are public well before kickoff, so it stands in
        # for information a bettor genuinely has. Live inference has no such
        # field and falls back to last week's starter.
        home_qb = _qb_id(game.get("home_qb_id")) or home_form.last_qb
        away_qb = _qb_id(game.get("away_qb_id")) or away_form.last_qb

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
            # ---- quarterback
            "qb_value_home": quarterbacks.value(home_qb),
            "qb_value_away": quarterbacks.value(away_qb),
            "qb_experience_home": quarterbacks.experience(home_qb),
            "qb_experience_away": quarterbacks.experience(away_qb),
            "qb_change_home": float(
                home_form.last_qb is not None and home_qb != home_form.last_qb),
            "qb_change_away": float(
                away_form.last_qb is not None and away_qb != away_form.last_qb),
            # ---- opponent-adjusted efficiency
            "off_epa_adj_home": home_form.off_adj.get(),
            "def_epa_adj_home": home_form.def_adj.get(),
            "off_epa_adj_away": away_form.off_adj.get(),
            "def_epa_adj_away": away_form.def_adj.get(),
            # ---- the pieces raw margin hides
            "st_epa_home": home_form.st.get(),
            "st_epa_away": away_form.st.get(),
            "turnover_luck_home": home_form.turnover_luck.get(),
            "turnover_luck_away": away_form.turnover_luck.get(),
            "pythagorean_home": home_form.pythagorean(),
            "pythagorean_away": away_form.pythagorean(),
            "form_home": home_form.form.get(),
            "form_away": away_form.form.get(),
            **{
                f"{stem}_{side}": form.situational_value(stem)
                for stem in SITUATIONAL
                for side, form in (("home", home_form), ("away", away_form))
            },
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
        row["qb_value_diff"] = _diff(row["qb_value_home"], row["qb_value_away"])
        row["epa_adj_diff"] = _diff(
            row["off_epa_adj_home"] - row["def_epa_adj_home"],
            row["off_epa_adj_away"] - row["def_epa_adj_away"],
        )
        row["st_epa_diff"] = _diff(row["st_epa_home"], row["st_epa_away"])
        row["turnover_luck_diff"] = _diff(
            row["turnover_luck_home"], row["turnover_luck_away"])
        row["pythagorean_diff"] = _diff(row["pythagorean_home"], row["pythagorean_away"])
        row["form_diff"] = _diff(row["form_home"], row["form_away"])
        for stem in ("third_down", "red_zone", "explosive"):
            row[f"{stem}_diff"] = _diff(row[f"{stem}_home"], row[f"{stem}_away"])

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

            home_form.points_for += hs
            home_form.points_against += as_
            away_form.points_for += as_
            away_form.points_against += hs
            home_form.form.update(hs - as_)
            away_form.form.update(as_ - hs)

            detail = (team_game_stats or {}).get(str(game.get("game_id"))) or {}
            # Credit the game's quarterback play to the same identity the row
            # was built from, so value lookups and the change flag agree.
            _absorb(detail.get(home), home_form, away_form, quarterbacks, home_qb)
            _absorb(detail.get(away), away_form, home_form, quarterbacks, away_qb)

    frame = pd.DataFrame(out)
    if not frame.empty:
        for col in NUMERIC_FEATURES + MARKET_FEATURES:
            if col not in frame.columns:
                frame[col] = np.nan
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame


def _diff(a, b) -> float:
    """Difference that propagates missingness instead of inventing a zero."""
    if a is None or b is None:
        return np.nan
    a, b = float(a), float(b)
    return np.nan if (a != a or b != b) else a - b


def _qb_id(value) -> str | None:
    """Normalise a quarterback id, treating blanks and NaN as unknown."""
    if value is None or value != value:
        return None
    text = str(value).strip()
    return text or None


def _absorb(stats: dict | None, team: _TeamForm, opponent: _TeamForm,
            quarterbacks: _QbTracker, starter: str | None = None) -> None:
    """Fold one completed game's detail into a team's rolling state.

    Efficiency is adjusted for the opponent *before* being stored, using that
    opponent's rating as it stood ahead of this game. Storing raw numbers and
    adjusting later would either leak future information or require replaying
    the whole season on every read.
    """
    if not stats:
        return

    opp_def = opponent.def_adj.get(0.0)
    opp_off = opponent.off_adj.get(0.0)
    opp_def = 0.0 if opp_def != opp_def else opp_def
    opp_off = 0.0 if opp_off != opp_off else opp_off

    off = stats.get("off_epa")
    if off is not None and off == off:
        # Moving the ball on a good defence counts for more than on a bad one.
        team.off_adj.update(float(off) - opp_def)
    deff = stats.get("def_epa")
    if deff is not None and deff == deff:
        team.def_adj.update(float(deff) - opp_off)

    team.st.update(stats.get("st_epa"))
    team.turnover_luck.update(stats.get("turnover_luck"))
    for stem, key in SITUATIONAL.items():
        team.situational[stem].update(stats.get(key))

    # ``qb_epa`` is the team's production across every dropback in the game, so
    # crediting it to the starter reads as "the quarterback play this team got
    # with X under centre" rather than one passer's personal average.
    qb_id = starter or _qb_id(stats.get("qb_id"))
    if qb_id:
        quarterbacks.update(qb_id, stats.get("qb_epa"), stats.get("qb_dropbacks"))
        team.last_qb = str(qb_id)


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
