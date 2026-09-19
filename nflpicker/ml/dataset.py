"""Assembling the training frame.

This lived inside the CLI command, which was fine while training was something
a person ran. It is now also a scheduled job, and two callers building their own
training frame is precisely how a feature ends up present in one path and
missing in the other — the failure this project has already had twice. So the
frame is built here, once, and both callers use it.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from .. import db
from .features import build_features, epa_by_game_from_pbp

# Play-by-play starts here; asking for older seasons just 404s.
PBP_FIRST_SEASON = 1999


def _quiet(*_args, **_kwargs) -> None:
    """Default progress sink: a scheduled run has no one to print to."""


def from_nflverse(
    since: int,
    *,
    with_epa: bool = True,
    progress: Callable[..., None] = _quiet,
) -> pd.DataFrame:
    """Download game history and build the full feature frame.

    Play-by-play is fetched for every training season rather than the recent
    ones. Partial coverage is worse than it sounds: if most rows lack these
    columns the model learns to ignore them, and the features look worthless
    when they are merely absent.
    """
    from ..availability import SnapShares, historical_index
    from ..sources.nflverse import NflverseSource

    nfl = NflverseSource()
    games = nfl.games()
    games = games[games["season"] >= since]
    rows = games.to_dict("records")
    for row in rows:
        row["home"] = row.pop("home_team", None)
        row["away"] = row.pop("away_team", None)
        row["neutral_site"] = str(row.get("location", "")).lower() == "neutral"
        row["season_type"] = "REG" if row.get("game_type") == "REG" else "POST"
    progress(f"  {len(rows)} games from {since}")

    if not with_epa:
        return build_features(rows)

    epa: dict = {}
    team_stats: dict = {}
    seasons = sorted(int(x) for x in games["season"].dropna().unique())
    seasons = [s for s in seasons if s >= max(since, PBP_FIRST_SEASON)]
    progress(f"  loading play-by-play for {len(seasons)} seasons…")

    ok = 0
    injuries, snaps = [], []
    for season in seasons:
        try:
            detail = nfl.game_team_stats(season)
            for row in detail.to_dict("records"):
                team_stats.setdefault(str(row["game_id"]), {})[row["team"]] = row
            epa.update(epa_by_game_from_pbp(nfl.play_by_play(season)))
            ok += 1
        except Exception as exc:  # noqa: BLE001 - one bad season must not stop the rest
            progress(f"    skipped {season}: {exc}")
        # Weekly reports are small and independent of play-by-play.
        report = nfl.injury_reports(season)
        if len(report):
            injuries.append(report)
        snap = nfl.snap_counts(season)
        if len(snap):
            snaps.append(snap)
    progress(f"  play-by-play loaded for {ok}/{len(seasons)} seasons "
             f"({len(team_stats)} games)")

    availability: dict = {}
    if injuries:
        shares = SnapShares(pd.concat(snaps) if snaps else None)
        availability = historical_index(pd.concat(injuries), shares)
        progress(f"  injury reports for {len(availability)} team-weeks")

    return build_features(rows, epa_by_game=epa, team_game_stats=team_stats,
                          availability=availability)


def from_database(progress: Callable[..., None] = _quiet) -> pd.DataFrame:
    """Build from what has already been stored locally, market lines included."""
    rows = db.query("SELECT * FROM games ORDER BY season, week, kickoff")
    consensus = db.query(
        "SELECT c.game_id, c.spread_home, c.total_points FROM consensus c "
        "JOIN (SELECT game_id, MAX(captured_at) m FROM consensus GROUP BY game_id) x "
        "ON x.game_id = c.game_id AND x.m = c.captured_at"
    )
    lines = {c["game_id"]: c for c in consensus}
    for row in rows:
        line = lines.get(row["game_id"])
        if line:
            row["spread_home"] = line["spread_home"]
            row["market_total"] = line["total_points"]
    progress(f"training on {len(rows)} games from the local database")
    return build_features(rows)
