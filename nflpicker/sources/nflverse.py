"""nflverse adapter: historical results with market lines, plus play-by-play EPA.

``games.csv`` is the backbone of model training — it carries final scores *and*
the historical closing spread/total, rest days, roof, surface and weather for
every game since 1999.  Play-by-play parquet is optional and only pulled for
recent seasons, because it is two orders of magnitude larger.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..config import get_config
from ..teams import try_resolve
from .base import HttpClient, SourceError

GAMES_CSV = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
PBP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{season}.parquet"
)

# Enough for efficiency ratings. The richer per-game extraction asks for more.
PBP_BASE_COLUMNS = [
    "game_id", "season", "week", "posteam", "defteam", "home_team", "away_team",
    "play_type", "epa", "success", "pass", "rush", "wp", "half_seconds_remaining",
    "yards_gained", "down",
]

# Columns we keep from games.csv. Anything missing is tolerated.
GAME_COLUMNS = [
    "game_id", "season", "game_type", "week", "gameday", "gametime",
    "away_team", "home_team", "away_score", "home_score", "location",
    "result", "total", "overtime", "away_rest", "home_rest",
    "away_moneyline", "home_moneyline", "spread_line", "total_line",
    "div_game", "roof", "surface", "temp", "wind",
    "away_qb_id", "home_qb_id", "away_qb_name", "home_qb_name", "stadium",
]


class NflverseSource:
    def __init__(self, client: HttpClient | None = None) -> None:
        self.http = client or HttpClient()
        self.cache_dir: Path = get_config().cache_dir / "nflverse"

    # ---------------------------------------------------------------- games
    def games(self, *, cache_ttl: float = 21600.0) -> pd.DataFrame:
        """Every NFL game since 1999 with scores and historical market lines."""
        dest = self.cache_dir / "games.csv"
        self.http.download(GAMES_CSV, dest, cache_ttl=cache_ttl)
        df = pd.read_csv(dest, low_memory=False)
        keep = [c for c in GAME_COLUMNS if c in df.columns]
        df = df[keep].copy()
        for side in ("home_team", "away_team"):
            if side in df.columns:
                df[side] = df[side].map(try_resolve)
        df = df.dropna(subset=["home_team", "away_team", "season", "week"])
        df["season"] = df["season"].astype(int)
        df["week"] = df["week"].astype(int)
        if "div_game" in df.columns:
            df["div_game"] = df["div_game"].fillna(0).astype(int)
        df["kickoff"] = pd.to_datetime(
            df.get("gameday", pd.Series(dtype=str)).astype(str)
            + " "
            + df.get("gametime", pd.Series(dtype=str)).fillna("13:00").astype(str),
            errors="coerce",
            utc=True,
        )
        return df.sort_values(["season", "week", "kickoff"]).reset_index(drop=True)

    # ----------------------------------------------------------- play-by-play
    def play_by_play(
        self,
        season: int,
        *,
        cache_ttl: float = 43200.0,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """Season play-by-play.

        The file is 372 columns wide, so only the needed subset is read.
        Callers that want more than the efficiency basics — quarterback
        production, turnovers, special teams — pass their own ``columns``.
        """
        dest = self.cache_dir / f"pbp_{season}.parquet"
        try:
            self.http.download(PBP_URL.format(season=season), dest, cache_ttl=cache_ttl)
        except SourceError as exc:
            raise SourceError(f"play-by-play for {season} unavailable: {exc}") from exc
        cols = columns if columns is not None else PBP_BASE_COLUMNS
        try:
            df = pd.read_parquet(dest, columns=cols)
        except Exception:  # noqa: BLE001 - column set drifts between seasons
            df = pd.read_parquet(dest)
        return df

    def game_team_stats(self, season: int) -> pd.DataFrame:
        """One row per (game, team) with the raw ingredients for market-blind
        features: efficiency, quarterback production, special teams and the
        turnover detail needed to separate skill from fumble luck."""
        return extract_game_team_stats(
            self.play_by_play(season, columns=GAME_STAT_COLUMNS)
        )

    def team_epa(self, season: int, through_week: int | None = None) -> pd.DataFrame:
        """Season-to-date offensive and defensive EPA/play per team.

        Garbage time (win probability already past 90/10) is excluded — those
        snaps are real but they are not predictive of the next game.
        """
        pbp = self.play_by_play(season)
        if through_week is not None and "week" in pbp.columns:
            pbp = pbp[pbp["week"] <= through_week]
        return aggregate_team_epa(pbp)


def aggregate_team_epa(pbp: pd.DataFrame) -> pd.DataFrame:
    """Offense/defense EPA per play, split by pass and rush."""
    if pbp.empty:
        return pd.DataFrame(
            columns=["team", "off_epa", "def_epa", "off_pass_epa", "off_rush_epa",
                     "def_pass_epa", "def_rush_epa", "off_success", "def_success", "plays"]
        )

    df = pbp.copy()
    if "play_type" in df.columns:
        df = df[df["play_type"].isin(["pass", "run"])]
    if "epa" in df.columns:
        df = df[df["epa"].notna()]
    if "wp" in df.columns:
        df = df[df["wp"].between(0.10, 0.90) | df["wp"].isna()]
    for side in ("posteam", "defteam"):
        if side in df.columns:
            df[side] = df[side].map(try_resolve)
    df = df.dropna(subset=["posteam", "defteam"])
    if df.empty:
        return aggregate_team_epa(pd.DataFrame())

    is_pass = df["pass"] == 1 if "pass" in df.columns else df["play_type"].eq("pass")
    df = df.assign(is_pass=is_pass.fillna(False).astype(bool))
    if "success" not in df.columns:
        df["success"] = (df["epa"] > 0).astype(float)

    def _side(group_col: str, prefix: str) -> pd.DataFrame:
        grouped = df.groupby(group_col)
        out = pd.DataFrame(
            {
                f"{prefix}_epa": grouped["epa"].mean(),
                f"{prefix}_success": grouped["success"].mean(),
                "plays": grouped.size(),
            }
        )
        passes = df[df["is_pass"]].groupby(group_col)["epa"].mean()
        rushes = df[~df["is_pass"]].groupby(group_col)["epa"].mean()
        out[f"{prefix}_pass_epa"] = passes
        out[f"{prefix}_rush_epa"] = rushes
        return out

    off = _side("posteam", "off")
    dfn = _side("defteam", "def").drop(columns=["plays"])
    merged = off.join(dfn, how="outer")
    merged.index.name = "team"
    return merged.reset_index()


# Columns pulled for the richer per-game extraction. Kept explicit because the
# full play-by-play file is 372 columns wide and reading all of it for 24
# seasons is needless work.
GAME_STAT_COLUMNS = [
    "game_id", "season", "week", "posteam", "defteam", "home_team", "away_team",
    "play_type", "epa", "qb_epa", "qb_dropback", "passer_player_id",
    "passer_player_name", "success", "pass", "rush", "wp",
    "interception", "fumble", "fumble_lost",
    "punt_attempt", "field_goal_attempt", "kickoff_attempt", "extra_point_attempt",
    # Situational detail: how drives actually end, and how often a play breaks.
    "down", "ydstogo", "first_down", "yardline_100", "touchdown", "yards_gained",
    "sack", "penalty_yards", "penalty_team", "series_success",
]

# A gain of at least this many yards is an "explosive" play. Explosive-play rate
# is more stable week to week than yards per game and is one of the better
# public predictors of scoring.
EXPLOSIVE_YARDS = 20

# Inside the opponent's twenty.
RED_ZONE_YARDLINE = 20

# League-average fumble recovery rate. Recoveries are close to a coin flip, so a
# team's recovery share is mostly luck and should not be projected forward.
FUMBLE_RECOVERY_RATE = 0.5


def _rate(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Safe rate: no attempts means no rate, not zero."""
    out = numerator / denominator.replace(0, np.nan)
    return out.astype(float)


# Columns _situational always returns, so downstream code can rely on the shape
# rather than on a game happening to contain a third down.
SITUATIONAL_COLUMNS = (
    "third_down_rate", "third_down_attempts", "red_zone_td_rate",
    "explosive_rate", "sack_rate", "penalty_yards",
)


def _situational(df: pd.DataFrame, scrimmage: pd.DataFrame) -> pd.DataFrame:
    """Third-down, red-zone, explosive, sack and penalty rates per game.

    These are all *rates*, deliberately. Counts are dominated by how many
    possessions a team happened to get; rates are what carries from week to
    week.

    Every column is always present, even when nothing qualifies. Emitting a
    column only when the play type occurred made the whole extraction fail on a
    slice with no third downs — which a partial or in-progress file can be.
    """
    key = ["game_id", "posteam"]
    if scrimmage.empty:
        return pd.DataFrame(
            columns=list(SITUATIONAL_COLUMNS),
            index=pd.MultiIndex.from_arrays([[], []], names=key),
        )

    out = pd.DataFrame(index=scrimmage.groupby(key).size().index)
    for column in SITUATIONAL_COLUMNS:
        out[column] = np.nan

    # ---- third down: attempts and conversions
    third = scrimmage[scrimmage["down"] == 3]
    if not third.empty:
        attempts = third.groupby(key).size()
        converted = third.groupby(key)["first_down"].sum()
        out["third_down_rate"] = _rate(converted, attempts)
        out["third_down_attempts"] = attempts

    # ---- red zone: trips that end in a touchdown
    red = scrimmage[scrimmage["yardline_100"] <= RED_ZONE_YARDLINE]
    if not red.empty:
        red_plays = red.groupby(key).size()
        red_tds = red.groupby(key)["touchdown"].sum()
        out["red_zone_td_rate"] = _rate(red_tds, red_plays)

    # ---- explosives: how often a play breaks for real yardage
    explosive = (scrimmage["yards_gained"] >= EXPLOSIVE_YARDS).astype(float)
    out["explosive_rate"] = scrimmage.assign(_x=explosive).groupby(key)["_x"].mean()

    # ---- pressure: sacks taken per dropback
    dropbacks = df[df["qb_dropback"] == 1]
    if not dropbacks.empty:
        taken = dropbacks.groupby(key)["sack"].sum()
        attempts = dropbacks.groupby(key).size()
        out["sack_rate"] = _rate(taken, attempts)

    # ---- discipline: penalty yards charged to this team, per game
    if "penalty_team" in df.columns and "penalty_yards" in df.columns:
        penalties = df[df["penalty_team"].notna()]
        if not penalties.empty:
            charged = penalties.groupby(["game_id", "penalty_team"])["penalty_yards"].sum()
            charged.index = charged.index.set_names(key)
            out["penalty_yards"] = charged

    return out


def extract_game_team_stats(pbp: pd.DataFrame) -> pd.DataFrame:
    """Per (game, team) offensive stats, plus each team's opponent mirrored in.

    Defensive numbers are not computed separately: a team's defensive EPA in a
    game is, by construction, its opponent's offensive EPA in that same game. So
    everything is grouped once by possession team and then paired.
    """
    if pbp is None or pbp.empty:
        return pd.DataFrame()

    df = pbp.copy()
    for col in GAME_STAT_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    df = df[GAME_STAT_COLUMNS]
    for side in ("posteam", "defteam", "home_team", "away_team"):
        df[side] = df[side].map(try_resolve)
    df = df.dropna(subset=["posteam", "defteam", "game_id"])
    if df.empty:
        return pd.DataFrame()

    is_scrimmage = df["play_type"].isin(["pass", "run"])
    scrimmage = df[is_scrimmage & df["epa"].notna()].copy()
    # Exclude garbage time: those snaps are real but they do not predict the
    # next game, and they are exactly where blowouts distort a season average.
    competitive = scrimmage[scrimmage["wp"].between(0.10, 0.90) | scrimmage["wp"].isna()]

    def _mean(frame: pd.DataFrame, mask=None, col: str = "epa") -> pd.Series:
        target = frame if mask is None else frame[mask]
        return target.groupby(["game_id", "posteam"])[col].mean()

    passes = competitive["pass"] == 1
    rushes = competitive["rush"] == 1
    stats = pd.DataFrame({
        "off_epa": _mean(competitive),
        "off_pass_epa": _mean(competitive, passes),
        "off_rush_epa": _mean(competitive, rushes),
        "off_success": _mean(competitive, col="success"),
        "plays": competitive.groupby(["game_id", "posteam"]).size(),
    })

    # ---- situational: third downs, red zone, explosives, sacks, penalties
    situational = _situational(df, scrimmage)
    stats = stats.join(situational, how="left")

    # ---- special teams: punts, field goals, kickoffs and extra points
    special = df[df["play_type"].isin(
        ["punt", "field_goal", "kickoff", "extra_point"]) & df["epa"].notna()]
    if not special.empty:
        stats["st_epa"] = special.groupby(["game_id", "posteam"])["epa"].mean()
        stats["st_plays"] = special.groupby(["game_id", "posteam"]).size()

    # ---- turnovers, split into the skill part and the coin-flip part
    turnovers = df.groupby(["game_id", "posteam"]).agg(
        fumbles=("fumble", "sum"),
        fumbles_lost=("fumble_lost", "sum"),
        interceptions=("interception", "sum"),
    )
    stats = stats.join(turnovers, how="outer")

    # ---- quarterback: the primary passer and his production this game
    dropbacks = df[(df["qb_dropback"] == 1) & df["passer_player_id"].notna()]
    if not dropbacks.empty:
        counts = dropbacks.groupby(
            ["game_id", "posteam", "passer_player_id", "passer_player_name"]
        ).size().rename("n").reset_index()
        primary = counts.sort_values("n").groupby(["game_id", "posteam"]).tail(1)
        primary = primary.set_index(["game_id", "posteam"])
        stats["qb_id"] = primary["passer_player_id"]
        stats["qb_name"] = primary["passer_player_name"]
        stats["qb_dropbacks"] = primary["n"]
        qb_epa = dropbacks[dropbacks["qb_epa"].notna()].groupby(
            ["game_id", "posteam"])["qb_epa"].mean()
        stats["qb_epa"] = qb_epa

    stats = stats.reset_index().rename(columns={"posteam": "team"})

    # ---- attach opponent, week and season, then mirror in the defensive side
    meta = df.groupby(["game_id", "posteam"]).agg(
        opponent=("defteam", "first"), season=("season", "first"),
        week=("week", "first"), home_team=("home_team", "first"),
    ).reset_index().rename(columns={"posteam": "team"})
    stats = stats.merge(meta, on=["game_id", "team"], how="left")
    stats["is_home"] = (stats["team"] == stats["home_team"]).astype(int)

    mirror_cols = ["off_epa", "off_pass_epa", "off_rush_epa", "off_success",
                   "fumbles", "fumbles_lost", "interceptions",
                   "third_down_rate", "red_zone_td_rate", "explosive_rate",
                   "sack_rate"]
    opponent_view = stats[["game_id", "team", *mirror_cols]].rename(
        columns={"team": "opponent", **{c: f"opp_{c}" for c in mirror_cols}}
    )
    stats = stats.merge(opponent_view, on=["game_id", "opponent"], how="left")

    # A team's defence is its opponent's offence in the same game.
    stats["def_epa"] = stats["opp_off_epa"]
    stats["def_pass_epa"] = stats["opp_off_pass_epa"]
    stats["def_rush_epa"] = stats["opp_off_rush_epa"]
    stats["def_success"] = stats["opp_off_success"]
    # A defence's situational numbers are what it allowed, i.e. the opponent's.
    stats["def_third_down_rate"] = stats["opp_third_down_rate"]
    stats["def_red_zone_td_rate"] = stats["opp_red_zone_td_rate"]
    stats["def_explosive_rate"] = stats["opp_explosive_rate"]
    stats["sack_rate_forced"] = stats["opp_sack_rate"]

    # ---- turnover margin, and how much of it was fumble luck
    stats["giveaways"] = stats["fumbles_lost"].fillna(0) + stats["interceptions"].fillna(0)
    stats["takeaways"] = (
        stats["opp_fumbles_lost"].fillna(0) + stats["opp_interceptions"].fillna(0)
    )
    # Expected margin credits half of every fumble rather than who fell on it.
    stats["expected_giveaways"] = (
        FUMBLE_RECOVERY_RATE * stats["fumbles"].fillna(0) + stats["interceptions"].fillna(0)
    )
    stats["expected_takeaways"] = (
        FUMBLE_RECOVERY_RATE * stats["opp_fumbles"].fillna(0)
        + stats["opp_interceptions"].fillna(0)
    )
    stats["turnover_margin"] = stats["takeaways"] - stats["giveaways"]
    stats["expected_turnover_margin"] = (
        stats["expected_takeaways"] - stats["expected_giveaways"]
    )
    stats["turnover_luck"] = stats["turnover_margin"] - stats["expected_turnover_margin"]

    drop = [c for c in stats.columns
            if c.startswith("opp_off_") or c in {
                "opp_third_down_rate", "opp_red_zone_td_rate",
                "opp_explosive_rate", "opp_sack_rate"}]
    return stats.drop(columns=drop)
