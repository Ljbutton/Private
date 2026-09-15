"""Who is actually playing, and what that is worth in points.

The model is trained on seasons of results, so it knows how good a team *has
been* — with the players who were on the field. It cannot know that a starter
was ruled out on Wednesday. Historical injury reports are not in the training
data, so this cannot be a learned feature; it is applied afterwards, as a points
adjustment to the projection.

Two things make that defensible rather than a fudge:

* The market already prices injuries. Adjusting our number therefore *removes*
  false disagreement rather than creating an edge — a model that ignores a
  ruled-out quarterback will claim a large edge on exactly the game it
  understands least.
* For quarterbacks the adjustment is not a constant. The app already tracks a
  rolling value per passer, so when a starter is out the cost is the measured
  gap between him and whoever is next, not a flat league-average guess.

Where this matters most is not this week's game, which has a line that already
reflects the news. It is the weeks *ahead*: survivor plans six weeks out, where
no market exists yet and the projection is all there is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .news.impact import POSITION_IMPACT
from .util import clamp

# Availability keywords, mapped to the share of a player's value the team loses.
STATUS_COST = {
    "out": 1.0,
    "injured reserve": 1.0,
    "ir": 1.0,
    "suspended": 1.0,
    "suspension": 1.0,
    "doubtful": 0.75,
    "questionable": 0.30,
    "probable": 0.05,
    "active": 0.0,
    "healthy": 0.0,
}

# A quarterback's rating is EPA per dropback; this converts it to points per
# game at roughly a starter's volume.
DROPBACKS_PER_GAME = 35.0

# No single team adjustment may exceed this. Injury reports in the NFL are
# strategic and often incomplete, and a runaway sum of questionable tags would
# swamp a rating built from a season of evidence.
MAX_TEAM_ADJUSTMENT = 7.0

# Beyond the top few absences the marginal cost falls off sharply — a team's
# fifth-most-important missing player is a backup by definition.
DECAY = 0.65


@dataclass
class TeamAvailability:
    team: str
    adjustment: float = 0.0            # points added to this team's projection
    missing: list[dict] = field(default_factory=list)
    qb_change: bool = False

    def to_dict(self) -> dict:
        return {
            "team": self.team,
            "adjustment": round(self.adjustment, 2),
            "qb_change": self.qb_change,
            "missing": self.missing[:8],
        }


# Suffixes and punctuation that differ between feeds for the same person.
_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}
_NAME_CLEAN = re.compile(r"[^A-Za-z ]")


def normalize_name(name: str | None) -> str:
    """A join key that survives the two spellings these feeds use.

    Injury reports say "Patrick Mahomes"; play-by-play says "P.Mahomes". There
    is no shared identifier between them, so both are reduced to first-initial
    plus surname — "P.MAHOMES" — which is the most specific form both sources
    can actually produce.

    This is not unique in principle: two players on one roster could share an
    initial and surname. It is used only to look up a quarterback's rating, and
    the cost of a rare collision is one mis-sized adjustment, which is a far
    better trade than ignoring injuries entirely.
    """
    if not name:
        return ""
    cleaned = _NAME_CLEAN.sub(" ", str(name).replace(".", " ").replace("-", " "))
    parts = [p for p in cleaned.upper().split() if p]
    parts = [p for p in parts if p not in _SUFFIXES] or parts
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0][0]}.{parts[-1]}"


def status_cost(status: str | None) -> float:
    """Share of a player's value lost, from a free-text injury status."""
    if not status:
        return 0.0
    text = str(status).strip().lower()
    best = 0.0
    for keyword, cost in STATUS_COST.items():
        if re.search(rf"\b{re.escape(keyword)}\b", text) and cost > best:
            best = cost
    return best


def _position_value(position: str | None) -> float:
    if not position:
        return 0.0
    return POSITION_IMPACT.get(str(position).strip().upper(), 0.25)


def quarterback_cost(
    team: str,
    out_players: list[dict],
    qb_values: dict[str, float] | None,
    depth: dict[str, list[str]] | None,
) -> tuple[float, bool]:
    """Points lost when a team's starting quarterback is unavailable.

    Uses the measured gap between the starter and his replacement when both are
    known. A backup who has played well is worth far more than the position's
    league-average replacement cost, and a team whose starter was already poor
    loses less than one whose starter is elite — a flat constant gets both wrong.
    """
    qbs = [p for p in out_players if str(p.get("position") or "").upper() == "QB"]
    if not qbs:
        return 0.0, False

    worst = max(qbs, key=lambda p: status_cost(p.get("status")))
    cost_share = status_cost(worst.get("status"))
    if cost_share <= 0:
        return 0.0, False

    values = qb_values or {}
    # One key identifies the starter for both the lookup and the exclusion.
    # Using different keys for each let him count as his own replacement, which
    # silently produced a zero-point gap for every injured starter.
    starter_key = worst.get("player_id") or worst.get("player") or ""
    starter = values.get(starter_key)
    backups = [
        values[q] for q in (depth or {}).get(team, [])
        if q in values and q != starter_key
    ]
    replacement = max(backups) if backups else None

    if starter is not None and replacement is not None:
        points = (starter - replacement) * DROPBACKS_PER_GAME
    else:
        # Fall back to the position's league-average cost when either side of
        # the comparison is unknown.
        points = POSITION_IMPACT["QB"]

    return max(0.0, points) * cost_share, cost_share >= 0.75


def team_adjustment(
    team: str,
    injuries: list[dict],
    *,
    qb_values: dict[str, float] | None = None,
    depth: dict[str, list[str]] | None = None,
) -> TeamAvailability:
    """Points to add to a team's projection given its current injury report."""
    out = TeamAvailability(team=team)
    unavailable = [i for i in injuries if status_cost(i.get("status")) > 0]
    if not unavailable:
        return out

    qb_points, qb_change = quarterback_cost(team, unavailable, qb_values, depth)
    out.qb_change = qb_change

    others: list[tuple[float, dict]] = []
    for player in unavailable:
        if str(player.get("position") or "").upper() == "QB":
            continue
        cost = _position_value(player.get("position")) * status_cost(player.get("status"))
        if cost > 0:
            others.append((cost, player))

    others.sort(key=lambda pair: pair[0], reverse=True)
    skill_points = sum(cost * (DECAY**i) for i, (cost, _) in enumerate(others))

    total = qb_points + skill_points
    out.adjustment = -clamp(total, 0.0, MAX_TEAM_ADJUSTMENT)
    out.missing = [
        {
            "player": p.get("player"),
            "position": p.get("position"),
            "status": p.get("status"),
            "cost": round(cost, 2),
        }
        for cost, p in ([(qb_points, q) for q in unavailable
                         if str(q.get("position") or "").upper() == "QB"][:1] + others)
    ]
    return out


def build_adjustments(
    injuries_by_team: dict[str, list[dict]],
    *,
    qb_values: dict[str, float] | None = None,
    depth: dict[str, list[str]] | None = None,
) -> dict[str, TeamAvailability]:
    return {
        team: team_adjustment(team, rows, qb_values=qb_values, depth=depth)
        for team, rows in injuries_by_team.items()
    }
