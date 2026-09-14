"""Survivor pool path optimiser.

The mistake that ends most survivor entries is taking the safest team available
this week and finding, in Week 12, that every remaining team is a coin flip.
So this does not pick a team — it plans a *path*.

Choosing one team per week, each usable once, to maximise the chance of
surviving every week is exactly a rectangular assignment problem: maximise
Σ log P(win) over weeks × teams.  ``scipy.optimize.linear_sum_assignment``
solves it optimally in milliseconds, so there is no reason to approximate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Probability floor. A team with no projection is unusable rather than a coin
# flip, which keeps the optimiser from "planning" around games it cannot see.
MIN_PROB = 1e-4
INFEASIBLE = 1e6

# Small enough to only separate paths of equal survival probability, never to
# override a genuinely better path.
EARLY_WEEK_TIEBREAK = 1e-4


@dataclass
class SurvivorEntry:
    week: int
    team: str
    opponent: str
    win_prob: float
    game_id: str | None = None
    kickoff: str | None = None

    def to_dict(self) -> dict:
        from ..teams import TEAMS

        return {
            "week": self.week,
            "team": self.team,
            "team_name": TEAMS[self.team].full_name if self.team in TEAMS else self.team,
            "opponent": self.opponent,
            "win_prob": round(self.win_prob, 4),
            "game_id": self.game_id,
            "kickoff": self.kickoff,
        }


@dataclass
class SurvivorPlan:
    season: int
    week: int
    horizon: int
    path: list[SurvivorEntry] = field(default_factory=list)
    survival_prob: float = 0.0
    recommendation: SurvivorEntry | None = None
    alternatives: list[dict] = field(default_factory=list)
    used_teams: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "season": self.season,
            "week": self.week,
            "horizon": self.horizon,
            "path": [e.to_dict() for e in self.path],
            "survival_prob": round(self.survival_prob, 4),
            "recommendation": self.recommendation.to_dict() if self.recommendation else None,
            "alternatives": self.alternatives,
            "used_teams": self.used_teams,
            "note": self.note,
        }


def _solve(weeks: list[int], teams: list[str], prob: dict[tuple[int, str], float],
           forced: tuple[int, str] | None = None) -> tuple[list[tuple[int, str]], float] | None:
    """Optimal week→team assignment maximising the product of win probabilities."""
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    if not weeks or not teams:
        return None
    # A rectangular assignment returns only min(rows, cols) pairs, silently
    # choosing which weeks to leave out — including, in the worst case, the week
    # being planned. A path that skips a week is not a path, so refuse here and
    # let the caller shorten the horizon deliberately.
    if len(teams) < len(weeks):
        return None
    cost = np.full((len(weeks), len(teams)), INFEASIBLE)
    for i, week in enumerate(weeks):
        # Tie-break toward surviving sooner. Two paths using the same teams have
        # the same survival product in any order, and the optimiser would other-
        # wise pick between them arbitrarily. Weighting earlier weeks by a hair
        # prefers the ordering that is safest now, which is strictly better: you
        # keep the entry alive while later weeks are still uncertain and can be
        # re-planned as projections firm up.
        recency = 1.0 + EARLY_WEEK_TIEBREAK * (len(weeks) - i)
        for j, team in enumerate(teams):
            p = prob.get((week, team))
            if p is not None and p > MIN_PROB:
                cost[i, j] = -math.log(p) * recency
    if forced is not None:
        fw, ft = forced
        if fw in weeks and ft in teams:
            i, j = weeks.index(fw), teams.index(ft)
            if cost[i, j] >= INFEASIBLE:
                return None
            # Pin the choice by making every other option in that week impossible.
            cost[i, :] = INFEASIBLE
            cost[i, j] = -math.log(max(prob[(fw, ft)], MIN_PROB))

    rows, cols = linear_sum_assignment(cost)
    chosen: list[tuple[int, str]] = []
    for r, c in zip(rows, cols, strict=True):
        if cost[r, c] >= INFEASIBLE:
            return None  # no feasible full path
        chosen.append((weeks[r], teams[c]))
    if len(chosen) != len(weeks):
        return None      # every week must be covered
    chosen.sort()
    # Report the true survival product. The tie-break weighting exists only to
    # order the search; quoting the weighted cost back would be wrong.
    survival = 1.0
    for week, team in chosen:
        survival *= prob[(week, team)]
    return chosen, survival


def plan_survivor(
    season: int,
    week: int,
    games_by_week: dict[int, list[dict]],
    *,
    used_teams: list[str] | None = None,
    horizon: int = 6,
    max_alternatives: int = 4,
) -> SurvivorPlan:
    """Plan the next ``horizon`` weeks.

    ``games_by_week`` maps week -> games, each with home/away/home_win_prob.
    ``used_teams`` are teams already spent in your pool.

    The horizon is capped deliberately: projections five weeks out are far less
    reliable than this week's, and planning twenty weeks ahead optimises against
    noise.  Six weeks is enough to stop the "burned my best team early" failure
    without pretending to know Week 17.
    """
    used = [t.upper() for t in (used_teams or [])]
    weeks = sorted(w for w in games_by_week if w >= week)[:horizon]
    plan = SurvivorPlan(season=season, week=week, horizon=len(weeks), used_teams=used)
    if not weeks:
        plan.note = "No remaining games to plan."
        return plan

    prob: dict[tuple[int, str], float] = {}
    meta: dict[tuple[int, str], dict] = {}
    teams: set[str] = set()
    for w in weeks:
        for game in games_by_week.get(w, []):
            home_prob = game.get("home_win_prob")
            if home_prob is None:
                continue
            for team, opponent, p in (
                (game["home"], game["away"], float(home_prob)),
                (game["away"], game["home"], 1.0 - float(home_prob)),
            ):
                if team in used:
                    continue
                prob[(w, team)] = p
                meta[(w, team)] = {
                    "opponent": opponent,
                    "game_id": game.get("game_id"),
                    "kickoff": game.get("kickoff"),
                }
                teams.add(team)

    team_list = sorted(teams)
    solution = _solve(weeks, team_list, prob)
    if solution is None:
        # Not enough distinct teams for the full horizon: shorten until solvable.
        for shorter in range(len(weeks) - 1, 0, -1):
            solution = _solve(weeks[:shorter], team_list, prob)
            if solution is not None:
                weeks = weeks[:shorter]
                plan.horizon = shorter
                plan.note = "Horizon shortened: not enough unused teams for a full path."
                break
    if solution is None:
        plan.note = "No feasible survivor path — too many teams already used."
        return plan

    chosen, survival = solution
    plan.path = [
        SurvivorEntry(week=w, team=t, win_prob=prob[(w, t)], **meta[(w, t)])
        for w, t in chosen
    ]
    plan.survival_prob = survival
    plan.recommendation = next((e for e in plan.path if e.week == week), None)

    # What does taking a different team this week actually cost over the horizon?
    # That is the number that matters, not this week's win probability alone.
    candidates = sorted(
        ((t, prob[(week, t)]) for t in team_list if (week, t) in prob),
        key=lambda pair: pair[1],
        reverse=True,
    )
    alternatives: list[dict] = []
    for team, p in candidates:
        if plan.recommendation and team == plan.recommendation.team:
            continue
        forced = _solve(weeks, team_list, prob, forced=(week, team))
        if forced is None:
            continue
        _, alt_survival = forced
        alternatives.append(
            {
                "team": team,
                "opponent": meta[(week, team)]["opponent"],
                "win_prob": round(p, 4),
                "path_survival": round(alt_survival, 4),
                "cost": round(survival - alt_survival, 4),
            }
        )
        if len(alternatives) >= max_alternatives:
            break
    plan.alternatives = alternatives
    return plan


def future_value(games_by_week: dict[int, list[dict]], team: str, from_week: int,
                 horizon: int = 8) -> float:
    """Best single-game win probability this team offers over the horizon.

    A quick "how much am I giving up by spending this team now" read for the UI.
    """
    best = 0.0
    for w in sorted(games_by_week):
        if w <= from_week or w > from_week + horizon:
            continue
        for game in games_by_week[w]:
            prob = game.get("home_win_prob")
            if prob is None:
                continue
            if game["home"] == team:
                best = max(best, float(prob))
            elif game["away"] == team:
                best = max(best, 1.0 - float(prob))
    return best
