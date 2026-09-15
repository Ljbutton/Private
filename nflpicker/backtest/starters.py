"""Does resolving the announced starter actually beat using last week's?

The starter feature shipped with its mechanism verified and its value assumed:
it was checked that Atlanta, with both quarterbacks listed Out, falls through
the depth chart to the third. Nobody checked whether that is worth anything.

This measures it, and the experiment has to be built carefully, because the
feature never touches training at all. Historical games carry the starter who
actually played, so the model always trains on the truth; the substitution only
applies to games that have not kicked off. The question is therefore not "does
the model improve" but the narrower one it actually turns on:

    for an upcoming game, does walking the depth chart past the injury report
    identify the real starter more often than assuming last week's?

So we replay history as if each game were upcoming -- taking only the injury
report and depth chart as they stood, never the result -- and compare both
answers against who started.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from ..availability import depth_chart_backups, normalize_name
from ..starters import expected_starters
from ..teams import try_resolve

# Injury reports begin in 2009; earlier seasons have nothing to resolve from.
FIRST_SEASON = 2009


@dataclass
class StarterAccuracy:
    """How often each rule named the quarterback who actually started."""

    n: int = 0                      # team-games both rules could answer
    last_week_right: int = 0
    announced_right: int = 0
    disagreed: int = 0              # the two rules named different players
    announced_right_when_disagreed: int = 0
    last_week_right_when_disagreed: int = 0
    seasons: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        def rate(hits: int, total: int) -> float | None:
            return round(hits / total, 4) if total else None

        return {
            "n": self.n,
            "seasons": self.seasons,
            "last_week_rate": rate(self.last_week_right, self.n),
            "announced_rate": rate(self.announced_right, self.n),
            "disagreed": self.disagreed,
            # The only rows where the feature can change anything. Everywhere
            # else the two rules agree and the substitution is a no-op, so a
            # headline rate over all games dilutes the result toward zero and
            # makes a real effect look like nothing.
            "announced_rate_when_disagreed":
                rate(self.announced_right_when_disagreed, self.disagreed),
            "last_week_rate_when_disagreed":
                rate(self.last_week_right_when_disagreed, self.disagreed),
        }


def _starters_by_game(games: pd.DataFrame) -> dict[tuple[int, int, str], str]:
    """(season, week, team) -> the quarterback who actually started."""
    out: dict[tuple[int, int, str], str] = {}
    for row in games.to_dict("records"):
        season, week = row.get("season"), row.get("week")
        if season is None or week is None:
            continue
        for side in ("home", "away"):
            team = try_resolve(row.get(f"{side}_team"))
            name = normalize_name(row.get(f"{side}_qb_name"))
            if team and name:
                out[(int(season), int(week), team)] = name
    return out


def _injuries_by_team(report: pd.DataFrame, week: int) -> dict[str, list[dict]]:
    """The report as it stood for one week, in the shape the resolver wants."""
    by_team: dict[str, list[dict]] = {}
    if report is None or len(report) == 0:
        return by_team
    subset = report[report["week"] == week] if "week" in report.columns else report
    for row in subset.to_dict("records"):
        team = try_resolve(row.get("team"))
        if not team:
            continue
        by_team.setdefault(team, []).append({
            "player": normalize_name(row.get("full_name") or row.get("player_name")),
            "position": row.get("position"),
            "status": row.get("report_status") or row.get("status"),
        })
    return by_team


def measure(
    seasons: list[int] | None = None,
    *,
    progress: Callable[..., None] = lambda *_a, **_k: None,
) -> StarterAccuracy:
    """Replay history one week at a time and score both rules."""
    from ..sources.nflverse import NflverseSource

    nfl = NflverseSource()
    all_games = nfl.games()
    seasons = seasons or sorted(
        int(s) for s in all_games["season"].dropna().unique() if int(s) >= FIRST_SEASON
    )

    actual = _starters_by_game(all_games)
    result = StarterAccuracy(seasons=list(seasons))

    for season in seasons:
        try:
            report = nfl.injury_reports(season)
            charts = nfl.depth_charts(season)
        except Exception as exc:  # noqa: BLE001 - a missing season is not a failure
            progress(f"  skipped {season}: {exc}")
            continue
        if report is None or len(report) == 0 or charts is None or len(charts) == 0:
            progress(f"  skipped {season}: no injury or depth data")
            continue

        weeks = sorted({int(w) for w in charts["week"].dropna().unique()})
        for week in weeks:
            if week <= 1:
                continue        # no previous week to fall back to
            chart = charts[charts["week"] == week]
            depth = {
                team: players
                for (_s, _w, team), players in depth_chart_backups(chart, "QB").items()
            }
            if not depth:
                continue

            expected = expected_starters(depth, _injuries_by_team(report, week))
            for team, pick in expected.items():
                truth = actual.get((season, week, team))
                # Last week's starter is the fallback the feature replaced. A
                # team on bye has no previous week, so it is not a fair test.
                previous = actual.get((season, week - 1, team))
                if not truth or not previous:
                    continue

                result.n += 1
                last_ok = previous == truth
                announced_ok = pick.player == truth
                result.last_week_right += int(last_ok)
                result.announced_right += int(announced_ok)
                if previous != pick.player:
                    result.disagreed += 1
                    result.announced_right_when_disagreed += int(announced_ok)
                    result.last_week_right_when_disagreed += int(last_ok)
        progress(f"  {season}: {result.n} team-games scored")

    return result
