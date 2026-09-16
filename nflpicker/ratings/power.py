"""The single power rating everything downstream reads.

A power rating is a forecast, not a summary. The test it has to pass is: freeze
it after week W, predict every remaining game of that season, and see how close
you get. Measured that way over 20,007 rest-of-season games from 2006 to 2025,
the formula this replaced was **worse than using Elo alone**:

    formula                       MAE     corr   straight-up   slope
    Elo + EPA (what was here)   11.145    0.320     62.3%       0.66
    Elo alone                   10.903    0.327     62.5%        --
    0.50*Elo + 3.0*Pythagorean  10.930    0.335     62.2%       1.59
    0.80*Elo + 4.8*Pythagorean  10.864    0.335     62.9%       1.00

"slope" is the regression of actual margin on the rating's own prediction; 1.0
means a rating that reads +7 describes a team that wins by 7. The old formula
was over-dispersed (0.66) because EPA added spread that was mostly noise, and
the straightforward fitted replacement was *under*-dispersed (1.59) because a
squared-error fit shrinks toward the mean. Scaling the fitted pair up by 1.6
fixes the scale, and since a positive scalar cannot reorder anything it is free:
it is also the RMSE optimum and better on both MAE and straight-up.

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

Quarterback value is now in, and it is the largest single thing that was
missing: MAE 10.850 -> 10.799, correlation 0.331 -> 0.344, straight-up 62.7%
-> 63.1% over 18,218 rest-of-season games. A rating built from results alone
cannot know that the team which went 4-2 did it with a backup, and that is
exactly the case it was getting wrong. See QB_VALUE_POINTS for the fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..teams import ABBRS
from ..util import clamp
from .efficiency import LEAGUE_POINTS_PER_GAME, TeamEfficiency, shrink

# League-average home-field advantage in points. Modern NFL sits near 1.8.
HOME_FIELD_POINTS = 1.8

# Fitted against rest-of-season margin, then scaled so the rating means what it
# says. The ridge fit gives 0.56 and 3.4, but a squared-error fit deliberately
# under-disperses: at those values the regression of actual margin on the
# rating has a slope of 1.59, i.e. a rating that reads +7 is describing a team
# that actually wins by 11. Scaling the pair up by 1.6 puts that slope at 1.00
# and, because ordering is untouched, costs nothing -- it is simultaneously the
# RMSE optimum (13.928) and better on MAE and straight-up than the fitted
# values. See the module docstring for the table.
ELO_SHRINK = 0.80
PYTHAGOREAN_POINTS = 4.8

# Below this many games a team's points for and against are noise, and the
# Pythagorean term is faded in rather than trusted from one blowout.
PYTHAGOREAN_FULL_AT = 6.0

# Straight win-loss record, on top of Elo and point differential.
#
# Measured the same way as everything else here -- freeze after week W, predict
# the rest of that season -- over 14,054 games from 2006-2026:
#
#     h2h weight     MAE     corr   straight-up
#            0.0  10.9135   0.3487     62.91%
#            1.5  10.9002   0.3473     62.89%
#            3.0  10.8985   0.3458     62.64%
#
# It is a wash: MAE improves by about a hundredth of a point, correlation and
# straight-up give back the same order of nothing. That is the expected result
# -- Elo is already a record, so asking again adds little.
#
# 1.0 is a deliberate middle. Zero is what this was, and a table where the 3-7
# team sits above the 7-3 one on equal point differential is a table nobody
# believes. Above about 2 the correlation cost stops being noise. One point of
# spread is enough to break that tie and not enough to reorder anything that
# point differential has a real opinion about.
HEAD_TO_HEAD_POINTS = 1.0

# Quarterback value, in points of rating per point of shrunk EPA per dropback.
#
# The module docstring has called this "known gain not taken" since the rating
# was rebuilt. Measured the same way as everything else -- freeze after week W,
# predict the rest of that season, 18,218 games from 2002-2026:
#
#     qb points     MAE     corr   straight-up
#           0.0  10.8499  0.3310     62.69%
#           7.0  10.8019  0.3423     63.07%
#          10.0  10.7985  0.3439     63.12%
#          14.0  10.8096  0.3441     63.27%
#          18.0  10.8404  0.3431     63.17%
#
# A clear minimum at 10, and unlike the head-to-head term this one is not a
# wash: 0.05 points of MAE, 0.013 of correlation and 0.4 points of straight-up
# accuracy. It is the largest single improvement available to this rating, and
# it is available because a power rating built from results alone cannot know
# that the team which went 4-2 did it with a backup.
#
# One honest caveat about the estimator. The coefficient above was fitted on
# the feature pipeline's EWMA of quarterback EPA; the runtime reads the
# registry's volume-shrunk *mean* of the same quantity. Both shrink toward the
# league average by dropbacks and both sit on the same scale, so the
# coefficient transfers in shape -- but an EWMA leans harder on recent games,
# so the runtime term will move a little more slowly than the measurement did.
QB_VALUE_POINTS = 10.0


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
    games_played: dict[str, int] | None = None,
    win_loss: dict[str, tuple[int, int]] | None = None,
    qb_value: dict[str, float] | None = None,
) -> PowerRatings:
    """One rating per team: shrunk Elo plus Pythagorean expectation.

    `records` is team -> (points for, points against) so far this season. Left
    out, the rating is Elo alone, which is still better than the EPA blend this
    replaced — see the module docstring.

    `games_played` is team -> games behind those points. It is what turns a
    record into a scoring *rate*, so it is what the offence and defence ratings
    need when there is no EPA to build them from. Without it the count can only
    be estimated from the points themselves, which is fine for fading the
    Pythagorean term in but useless for a rate: dividing points by a games
    count derived from those same points gives every team the identical
    scoring profile.
    """
    efficiencies = efficiencies or {}
    records = records or {}
    games_played = games_played or {}
    win_loss = win_loss or {}
    qb_value = qb_value or {}
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
        counted = float(games_played.get(abbr) or 0.0)
        played = counted or (points_for + points_against) / 45.0
        pyth = pythagorean_expectation(points_for, points_against)
        if pyth is not None:
            # Faded in by games played: one blowout should not rank a team.
            trust = clamp(played / PYTHAGOREAN_FULL_AT, 0.0, 1.0)
            power += PYTHAGOREAN_POINTS * (pyth - 0.5) * trust

        # Expected points scored and allowed. EPA is the better estimate and is
        # used where it exists; where it does not, the team's own scoring record
        # stands in. Falling straight back to the league average instead meant
        # every team was identical and every game projected exactly 45 points --
        # a constant wearing the shape of a projection, which is worse than a
        # rough number because nothing about it looks wrong.
        # Who actually won, as opposed to who outscored. Faded in on the same
        # schedule as the Pythagorean term so one upset in week 1 does not
        # reorder the league.
        wins, losses = win_loss.get(abbr, (0, 0))
        if wins + losses:
            trust = clamp(played / PYTHAGOREAN_FULL_AT, 0.0, 1.0)
            power += HEAD_TO_HEAD_POINTS * (wins / (wins + losses) - 0.5) * trust

        # The quarterback the team has been playing. This is deliberately who
        # *started*, not who is expected to start on Sunday: an announced
        # change is priced separately by the availability layer, and counting
        # it here as well would charge for the same absence twice.
        qb = qb_value.get(abbr)
        if qb is not None and played > 0:
            trust = clamp(played / PYTHAGOREAN_FULL_AT, 0.0, 1.0)
            power += QB_VALUE_POINTS * float(qb) * trust

        eff = efficiencies.get(abbr)
        off_rating = LEAGUE_POINTS_PER_GAME
        def_rating = LEAGUE_POINTS_PER_GAME
        if eff is not None and eff.off_points is not None and eff.def_points is not None:
            sample = eff.plays / (eff.plays + 250.0) if eff.plays else 0.0
            off_rating += eff.off_points * sample
            def_rating += eff.def_points * sample
        elif counted > 0:
            # Faded in by games played, on the same schedule as the Pythagorean
            # term: one high-scoring afternoon is not an offence.
            trust = clamp(counted / PYTHAGOREAN_FULL_AT, 0.0, 1.0)
            off_rating += (points_for / counted - LEAGUE_POINTS_PER_GAME) * trust
            def_rating += (points_against / counted - LEAGUE_POINTS_PER_GAME) * trust

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
