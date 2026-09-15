"""The single power rating everything downstream reads.

A power rating is a forecast, not a summary. The test it has to pass is: freeze
it after week W, predict every remaining game of that season, and see how close
you get. Measured that way over 20,007 rest-of-season games from 2006 to 2025,
the formula this replaced was **worse than using Elo alone**:

    formula                      MAE     corr    straight-up
    Elo + EPA (what was here)  11.145    0.320      62.3%
    Elo alone                  10.903    0.327      62.5%
    Elo + Pythagorean          10.930    0.335      62.2%
    0.50*Elo + 3*Pythagorean   10.930    0.335      62.2%

The EPA blend was the problem. It ramped to 60% weight by week 10, and net EPA
turns out to carry almost nothing about the *rest of the season* once Elo and
point differential are in: adding it to the pair above moves MAE from 10.8350
to 10.8341. Four decimal places of nothing, for most of the rating's weight.

Two things replace it:

* **Elo is shrunk.** Regressing rest-of-season margin on the Elo difference
  gives a slope near 0.56, not 1.0 — Elo is a good ordering and an overconfident
  spread. It was being quoted at full strength.
* **Pythagorean expectation is added.** Points scored and allowed predict future
  results better than the results themselves, which is the actual complaint a
  power ranking exists to answer: a 1-1 team that won by 20 and lost by 2 is not
  the same as a 1-1 team that did the reverse.

EPA has not gone away — it still drives ``off_rating`` and ``def_rating``, which
project *totals*. It simply does not belong in the margin forecast.

Known gain not taken: quarterback value is the second-strongest predictor here
(1.23 points of spread, behind Elo's 2.77) and adding it reaches MAE 10.845 /
corr 0.343. It needs the quarterback tracker from the feature pipeline, which
this module has no access to yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..teams import ABBRS
from ..util import clamp
from .efficiency import LEAGUE_POINTS_PER_GAME, TeamEfficiency, shrink

# League-average home-field advantage in points. Modern NFL sits near 1.8.
HOME_FIELD_POINTS = 1.8

# Fitted against rest-of-season margin; see the module docstring. Elo is an
# excellent ordering quoted at roughly twice the spread it earns, and
# Pythagorean expectation on a 0-1 scale is worth about three points of margin
# end to end.
ELO_SHRINK = 0.50
PYTHAGOREAN_POINTS = 3.0

# Below this many games a team's points for and against are noise, and the
# Pythagorean term is faded in rather than trusted from one blowout.
PYTHAGOREAN_FULL_AT = 6.0


@dataclass
class TeamPower:
    team: str
    power: float = 0.0            # points better than average, neutral field
    pythagorean: float | None = None   # win expectation from points for/against
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


def pythagorean_expectation(points_for: float, points_against: float,
                            exponent: float = 2.37) -> float | None:
    """Win expectation implied by points scored and allowed.

    Point differential predicts future results better than the results
    themselves do, which is the whole complaint a power ranking answers: a 1-1
    team that won by 20 and lost by 2 is not the same team as one that did the
    reverse, and a table sorted by record cannot tell them apart.
    """
    total = points_for + points_against
    if total <= 0:
        return None
    pf = points_for ** exponent
    return pf / (pf + points_against ** exponent)


def build_power_ratings(
    elo_points: dict[str, float],
    efficiencies: dict[str, TeamEfficiency] | None = None,
    *,
    week: int = 1,
    elo_raw: dict[str, float] | None = None,
    records: dict[str, tuple[float, float]] | None = None,
) -> PowerRatings:
    """One rating per team: shrunk Elo plus Pythagorean expectation.

    `records` is team -> (points for, points against) so far this season. Left
    out, the rating is Elo alone, which is still better than the EPA blend this
    replaced — see the module docstring.
    """
    efficiencies = efficiencies or {}
    records = records or {}
    eff_points = shrink(efficiencies)
    # Kept only so the value is still reported; it no longer moves `power`.
    weight = efficiency_weight_for_week(week) if eff_points else 0.0

    teams: dict[str, TeamPower] = {}
    for abbr in ABBRS:
        elo_pts = elo_points.get(abbr, 0.0)
        eff_pts = eff_points.get(abbr)

        power = ELO_SHRINK * elo_pts
        pyth = None
        points_for, points_against = records.get(abbr, (0.0, 0.0))
        played = (points_for + points_against) / 45.0    # ~ a game's worth of points
        pyth = pythagorean_expectation(points_for, points_against)
        if pyth is not None:
            # Faded in by games played: one blowout should not rank a team.
            trust = clamp(played / PYTHAGOREAN_FULL_AT, 0.0, 1.0)
            power += PYTHAGOREAN_POINTS * (pyth - 0.5) * trust

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
            pythagorean=pyth,
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
