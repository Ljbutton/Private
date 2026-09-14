"""The single power rating everything downstream reads.

Blends Elo (fast-reacting, results-only) with EPA efficiency (slower, more
predictive).  The blend weight moves with the season: in Week 2 there is not
enough efficiency data to trust, by Week 10 there is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..teams import ABBRS
from ..util import clamp
from .efficiency import LEAGUE_POINTS_PER_GAME, TeamEfficiency, shrink

# League-average home-field advantage in points. Modern NFL sits near 1.8.
HOME_FIELD_POINTS = 1.8


@dataclass
class TeamPower:
    team: str
    power: float = 0.0            # points better than average, neutral field
    elo: float | None = None
    elo_points: float | None = None
    eff_points: float | None = None
    off_rating: float = LEAGUE_POINTS_PER_GAME   # expected points scored
    def_rating: float = LEAGUE_POINTS_PER_GAME   # expected points allowed
    off_epa: float | None = None
    def_epa: float | None = None
    plays: int = 0


@dataclass
class PowerRatings:
    teams: dict[str, TeamPower] = field(default_factory=dict)
    efficiency_weight: float = 0.0

    def get(self, team: str) -> TeamPower:
        return self.teams.get(team) or TeamPower(team=team)

    def margin(self, home: str, away: str, *, neutral_site: bool = False) -> float:
        """Projected home margin from power ratings alone."""
        edge = self.get(home).power - self.get(away).power
        return edge + (0.0 if neutral_site else HOME_FIELD_POINTS)

    def total(self, home: str, away: str) -> float:
        """Projected combined points: each offence against the other defence."""
        h, a = self.get(home), self.get(away)
        home_points = (h.off_rating + a.def_rating) / 2.0
        away_points = (a.off_rating + h.def_rating) / 2.0
        return home_points + away_points

    def ranked(self) -> list[TeamPower]:
        return sorted(self.teams.values(), key=lambda t: t.power, reverse=True)


def efficiency_weight_for_week(week: int) -> float:
    """How much to trust EPA versus Elo at a given point in the season."""
    if week <= 2:
        return 0.10
    return clamp(0.10 + 0.05 * (week - 2), 0.10, 0.60)


def build_power_ratings(
    elo_points: dict[str, float],
    efficiencies: dict[str, TeamEfficiency] | None = None,
    *,
    week: int = 1,
    elo_raw: dict[str, float] | None = None,
) -> PowerRatings:
    """Combine Elo (in points) with shrunk EPA into one rating per team."""
    efficiencies = efficiencies or {}
    eff_points = shrink(efficiencies)
    weight = efficiency_weight_for_week(week) if eff_points else 0.0

    teams: dict[str, TeamPower] = {}
    for abbr in ABBRS:
        elo_pts = elo_points.get(abbr, 0.0)
        eff_pts = eff_points.get(abbr)
        power = elo_pts if eff_pts is None else (1 - weight) * elo_pts + weight * eff_pts

        eff = efficiencies.get(abbr)
        off_rating = LEAGUE_POINTS_PER_GAME
        def_rating = LEAGUE_POINTS_PER_GAME
        if eff is not None and eff.off_points is not None and eff.def_points is not None:
            sample = eff.plays / (eff.plays + 250.0) if eff.plays else 0.0
            off_rating += eff.off_points * sample
            def_rating += eff.def_points * sample

        teams[abbr] = TeamPower(
            team=abbr,
            power=power,
            elo=(elo_raw or {}).get(abbr),
            elo_points=elo_pts,
            eff_points=eff_pts,
            off_rating=off_rating,
            def_rating=def_rating,
            off_epa=eff.off_epa if eff else None,
            def_epa=eff.def_epa if eff else None,
            plays=eff.plays if eff else 0,
        )

    # Re-centre so the league average is exactly zero.
    mean_power = sum(t.power for t in teams.values()) / len(teams)
    for t in teams.values():
        t.power -= mean_power

    return PowerRatings(teams=teams, efficiency_weight=weight)
