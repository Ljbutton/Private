"""EPA-based efficiency ratings, expressed in points per game.

EPA per play is the most stable public measure of team quality, but it is not
on the same scale as a point spread.  Multiplying by plays per game puts it
there, so efficiency and Elo can be blended without fudge factors.
"""

from __future__ import annotations

from dataclasses import dataclass

PLAYS_PER_GAME = 63.0
LEAGUE_POINTS_PER_GAME = 22.5


@dataclass
class TeamEfficiency:
    team: str
    off_epa: float | None = None
    def_epa: float | None = None
    off_pass_epa: float | None = None
    off_rush_epa: float | None = None
    def_pass_epa: float | None = None
    def_rush_epa: float | None = None
    off_success: float | None = None
    def_success: float | None = None
    plays: int = 0

    @property
    def off_points(self) -> float | None:
        """Points per game above an average offence."""
        return None if self.off_epa is None else self.off_epa * PLAYS_PER_GAME

    @property
    def def_points(self) -> float | None:
        """Points per game *allowed* above average; negative is a good defence."""
        return None if self.def_epa is None else self.def_epa * PLAYS_PER_GAME

    @property
    def net_points(self) -> float | None:
        if self.off_points is None or self.def_points is None:
            return None
        return self.off_points - self.def_points


def from_epa_frame(df) -> dict[str, TeamEfficiency]:
    """Build efficiency records from :func:`nflverse.aggregate_team_epa` output."""
    out: dict[str, TeamEfficiency] = {}
    if df is None or len(df) == 0:
        return out
    for row in df.to_dict("records"):
        team = row.get("team")
        if not team:
            continue
        get = _reader(row)
        out[team] = TeamEfficiency(
            team=team,
            off_epa=get("off_epa"), def_epa=get("def_epa"),
            off_pass_epa=get("off_pass_epa"), off_rush_epa=get("off_rush_epa"),
            def_pass_epa=get("def_pass_epa"), def_rush_epa=get("def_rush_epa"),
            off_success=get("off_success"), def_success=get("def_success"),
            plays=int(get("plays") or 0),
        )
    return out


def _reader(row: dict):
    """Field reader bound to one row, returning None for missing and NaN values.

    Defined outside the loop so it binds this row explicitly rather than closing
    over a loop variable that later iterations would rebind.
    """

    def get(key: str) -> float | None:
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            return None
        return None if value != value else value  # NaN

    return get


def shrink(
    efficiencies: dict[str, TeamEfficiency],
    *,
    prior_plays: float = 250.0,
) -> dict[str, float]:
    """Regress each team's net efficiency toward the mean by sample size.

    Three games of EPA is mostly noise; shrinking by plays played stops Week 3
    outliers from dominating the power ratings.
    """
    out: dict[str, float] = {}
    for team, eff in efficiencies.items():
        net = eff.net_points
        if net is None:
            continue
        weight = eff.plays / (eff.plays + prior_plays) if eff.plays else 0.0
        out[team] = net * weight
    return out


def points_per_game(efficiencies: dict[str, TeamEfficiency]) -> dict[str, tuple[float, float]]:
    """Per-team (expected points scored, expected points allowed) per game."""
    out: dict[str, tuple[float, float]] = {}
    for team, eff in efficiencies.items():
        off = eff.off_points
        deff = eff.def_points
        if off is None or deff is None:
            continue
        out[team] = (
            LEAGUE_POINTS_PER_GAME + off,
            LEAGUE_POINTS_PER_GAME + deff,
        )
    return out
