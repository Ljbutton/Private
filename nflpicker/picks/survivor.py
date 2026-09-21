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
    through_week: int | None = None
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
            "through_week": self.through_week,
            "path": [e.to_dict() for e in self.path],
            "survival_prob": round(self.survival_prob, 4),
            "recommendation": self.recommendation.to_dict() if self.recommendation else None,
            "alternatives": self.alternatives,
            "used_teams": self.used_teams,
            "note": self.note,
        }


# The end of the regular season. Pools that settle in week 17 set it back --
# week 18 rests starters and is the week a projection is worth least -- but a
# plan that stops early cannot be extended by its reader, and one that runs a
# week long can be ignored from the row above.
LAST_SURVIVOR_WEEK = 18


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
    through_week: int = LAST_SURVIVOR_WEEK,
    max_alternatives: int = 4,
) -> SurvivorPlan:
    """Plan from ``week`` to ``through_week`` inclusive.

    ``games_by_week`` maps week -> games, each with home/away/home_win_prob.
    ``used_teams`` are teams already spent in your pool.

    The horizon used to be "the next six weeks", on the reasoning that a
    projection ten weeks out is mostly noise. That reasoning is sound about the
    *projections* and wrong about the *problem*: survivor is a scheduling
    constraint, not a forecast. Each team may be spent once, so the cost of
    using a team this week is whichever future week wanted it -- and a planner
    that cannot see past week six cannot see that cost. Burning the team you
    needed in week 14 is exactly the failure survivor pools are built to
    punish, and a six-week window walks straight into it.

    Week 17 is the last week worth planning: most pools end there, and week 18
    is where teams rest starters and a projection means least.

    The far weeks are still noisy, and the plan is meant to be re-run -- what
    it is for is spending *this* week's team knowing what it costs later.
    """
    used = [t.upper() for t in (used_teams or [])]
    weeks = [w for w in sorted(games_by_week) if week <= w <= through_week]
    plan = SurvivorPlan(season=season, week=week, horizon=len(weeks),
                        through_week=weeks[-1] if weeks else None, used_teams=used)
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
                plan.through_week = weeks[-1]
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


# --------------------------------------------------------------- the tracker
# What the plan said at the start of the season against what actually got
# picked. A survivor pool is one long bet whose result arrives in instalments,
# and "was the optimiser right" is not answerable from any single week: the
# only question that settles it is which of the two runs busts first.

ORIGINAL_KEY = "survivor_original_plan"
USED_WEEKS_KEY = "survivor_used_weeks"


def _outcome(game: dict | None, team: str) -> str:
    """won, lost, tied, or not played yet."""
    if not game or game.get("status") != "final":
        return "pending"
    home, away = game.get("home"), game.get("away")
    hs, as_ = game.get("home_score"), game.get("away_score")
    if hs is None or as_ is None:
        return "pending"
    if hs == as_:
        return "tied"
    winner = home if hs > as_ else away
    return "won" if winner == team else "lost"


def _played(games: list[dict], week: int, team: str) -> dict | None:
    for game in games:
        if int(game["week"]) == int(week) and team in (game["home"], game["away"]):
            return game
    return None


def _walk(entries: list[dict], games: list[dict]) -> dict:
    """One run's weeks, and the week it went out on.

    A tie is survival in most pools and elimination in some. It is counted as
    survival here and labelled, rather than quietly resolved either way: the
    pool's rules decide, and the reader knows theirs.
    """
    rows = []
    out_week = None
    for entry in entries:
        week, team = int(entry["week"]), entry["team"]
        game = _played(games, week, team)
        result = _outcome(game, team)
        opponent = None
        if game:
            opponent = game["away"] if game["home"] == team else game["home"]
        rows.append({
            "week": week, "team": team, "opponent": opponent, "result": result,
            "score": (None if not game or game.get("home_score") is None else
                      f"{game['away']} {int(game['away_score'])}-"
                      f"{int(game['home_score'])} {game['home']}"),
        })
        if result == "lost" and out_week is None:
            out_week = week
    survived = [r for r in rows if r["result"] in ("won", "tied")]
    return {
        "weeks": rows,
        "out_week": out_week,
        "alive": out_week is None,
        "weeks_survived": len(survived),
    }


def elimination(used_weeks: dict, games: list[dict]) -> dict | None:
    """The week a run ended, or None while it is still going.

    Worked out from the picks every time it is asked for, and never written
    down. A pool entry is dead because of a result, and a result is a fact
    about one game and the team that was on it -- so correcting the pick that
    lost is not a special case to undo, it is a different question with a
    different answer. An elimination stored anywhere would outlive the pick
    that caused it and have to be cleared by hand.
    """
    entries = sorted(
        ({"week": int(w), "team": t} for t, w in (used_weeks or {}).items()),
        key=lambda e: e["week"])
    run = _walk(entries, games)
    if run["out_week"] is None:
        return None
    lost = next(r for r in run["weeks"]
                if r["week"] == run["out_week"] and r["result"] == "lost")
    return {**lost, "weeks_survived": run["weeks_survived"]}


def track(original: list[dict], used_weeks: dict, games: list[dict]) -> dict:
    """The original run and the picked one, side by side.

    `original` is the plan as first made -- entries with a week and a team.
    `used_weeks` is team -> the week it was actually spent in. Both are walked
    against the same finished games, so the comparison is between two runs and
    not between a plan and a scoreboard.
    """
    mine_entries = sorted(
        ({"week": int(w), "team": t} for t, w in (used_weeks or {}).items()),
        key=lambda e: e["week"])
    plan = _walk(list(original or []), games)
    mine = _walk(mine_entries, games)

    if not original:
        verdict = "No original run saved yet — it is kept the first time a plan is made."
    elif not mine_entries:
        verdict = "Nothing picked yet. Mark a team used on Picks and it starts here."
    elif plan["alive"] and mine["alive"]:
        # Nothing to say. Both columns are headed "alive · N survived" and
        # every row underneath is a tick, so a line announcing that neither
        # has gone out yet is the panel repeating its own table back at the
        # reader -- and in a panel this size it costs a row of the run.
        verdict = ""
    elif plan["alive"]:
        verdict = f"You went out in week {mine['out_week']}. The original run is still alive."
    elif mine["alive"]:
        verdict = f"The original run went out in week {plan['out_week']}. You are still alive."
    elif plan["out_week"] == mine["out_week"]:
        verdict = f"Both went out in week {plan['out_week']}."
    elif plan["out_week"] < mine["out_week"]:
        verdict = (f"The original run went out first, in week {plan['out_week']}; "
                   f"you lasted to week {mine['out_week']}.")
    else:
        verdict = (f"You went out first, in week {mine['out_week']}; the original run "
                   f"lasted to week {plan['out_week']}.")

    return {"original": plan, "mine": mine, "verdict": verdict}
