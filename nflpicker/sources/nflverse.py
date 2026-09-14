"""nflverse adapter: historical results with market lines, plus play-by-play EPA.

``games.csv`` is the backbone of model training — it carries final scores *and*
the historical closing spread/total, rest days, roof, surface and weather for
every game since 1999.  Play-by-play parquet is optional and only pulled for
recent seasons, because it is two orders of magnitude larger.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..config import get_config
from ..teams import try_resolve
from .base import HttpClient, SourceError

GAMES_CSV = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
PBP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{season}.parquet"
)

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
    def play_by_play(self, season: int, *, cache_ttl: float = 43200.0) -> pd.DataFrame:
        dest = self.cache_dir / f"pbp_{season}.parquet"
        try:
            self.http.download(PBP_URL.format(season=season), dest, cache_ttl=cache_ttl)
        except SourceError as exc:
            raise SourceError(f"play-by-play for {season} unavailable: {exc}") from exc
        cols = [
            "game_id", "season", "week", "posteam", "defteam", "home_team", "away_team",
            "play_type", "epa", "success", "pass", "rush", "wp", "half_seconds_remaining",
            "yards_gained", "down",
        ]
        try:
            df = pd.read_parquet(dest, columns=cols)
        except Exception:  # noqa: BLE001 - column set drifts between seasons
            df = pd.read_parquet(dest)
        return df

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
