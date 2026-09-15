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
    qb_in_model: bool = False      # the replacement is a feature, not an offset

    def to_dict(self) -> dict:
        return {
            "team": self.team,
            "adjustment": round(self.adjustment, 2),
            "qb_change": self.qb_change,
            "qb_in_model": self.qb_in_model,
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
    qb_priced: bool = False,
) -> TeamAvailability:
    """Points to add to a team's projection given its current injury report.

    ``qb_priced`` says the replacement starter has already been fed to the model
    as a feature, so the quarterback downgrade is inside the projection and must
    not be charged a second time here. Skill-position absences are still costed:
    those have no equivalent feature.
    """
    out = TeamAvailability(team=team)
    unavailable = [i for i in injuries if status_cost(i.get("status")) > 0]
    if not unavailable:
        return out

    qb_points, qb_change = quarterback_cost(team, unavailable, qb_values, depth)
    out.qb_change = qb_change
    if qb_priced:
        qb_points = 0.0
        out.qb_in_model = True

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


# ---------------------------------------------------------------- historical

# Snap share to assume for a listed player we have no participation data for.
# Injury reports skew toward players who actually play, so zero would be the
# wrong default; a middling share is closer.
DEFAULT_SNAP_SHARE = 0.45


class SnapShares:
    """Each player's participation, queryable as of any week.

    Built as a per-player history rather than a flat week-keyed table because
    of the case the whole feature exists for: a player who is *out* has no snap
    row that week. Keying on the week he is missing means the lookup misses for
    exactly the players who matter, silently falling back to a default — which
    is what it did before this class existed, for every injured quarterback.
    """

    def __init__(self, snap_counts=None) -> None:
        self._history: dict[tuple[int, str, str], list[tuple[int, float]]] = {}
        if snap_counts is None or len(snap_counts) == 0:
            return
        for row in snap_counts.to_dict("records"):
            season = int(row.get("season") or 0)
            week = int(row.get("week") or 0)
            team = row.get("team")
            player = normalize_name(row.get("player"))
            if not team or not player:
                continue
            share = max(
                float(row.get("offense_pct") or 0.0),
                float(row.get("defense_pct") or 0.0),
            )
            self._history.setdefault((season, team, player), []).append((week, share))

    def before(self, season: int, week: int, team: str, player: str) -> float | None:
        """Mean snap share over the weeks preceding ``week``.

        Strictly preceding: this week's participation would leak the answer,
        since a player hurt in warmups shows a share of zero.
        """
        history = self._history.get((season, team, player))
        if not history:
            return None
        prior = [share for w, share in history if w < week]
        return sum(prior) / len(prior) if prior else None

    def __len__(self) -> int:
        return len(self._history)


def historical_index(
    injuries,
    snap_counts=None,
    *,
    max_players: int = 10,
) -> dict[tuple[int, int, str], float]:
    """Availability cost per (season, week, team), in points.

    This is what makes availability a *learned* feature rather than a
    correction applied afterwards: the weekly reports exist back to 2009, so a
    model can see who was listed alongside what happened.

    A player's weight is his snap share times the position's value, rather than
    the position alone. A constant says every starting receiver matters
    equally; snap share is the measurement that constant was standing in for.
    """
    out: dict[tuple[int, int, str], float] = {}
    if injuries is None or len(injuries) == 0:
        return out

    shares = snap_counts if isinstance(snap_counts, SnapShares) else SnapShares(snap_counts)
    grouped: dict[tuple[int, int, str], list[float]] = {}

    for row in injuries.to_dict("records"):
        season = int(row.get("season") or 0)
        week = int(row.get("week") or 0)
        team = row.get("team")
        if not team:
            continue
        status = status_cost(row.get("report_status"))
        if status <= 0:
            continue

        player = normalize_name(row.get("full_name"))
        share = shares.before(season, week, team, player)
        if share is None:
            share = DEFAULT_SNAP_SHARE
        weight = _position_value(row.get("position"))
        grouped.setdefault((season, week, team), []).append(weight * share * status)

    for key, costs in grouped.items():
        costs.sort(reverse=True)
        total = sum(cost * (DECAY**i) for i, cost in enumerate(costs[:max_players]))
        out[key] = -clamp(total, 0.0, MAX_TEAM_ADJUSTMENT)
    return out


def depth_chart_backups(depth_charts, position: str = "QB") -> dict[tuple[int, int, str], list[str]]:
    """(season, week, team) -> players at a position, ordered by depth."""
    out: dict[tuple[int, int, str], list[str]] = {}
    if depth_charts is None or len(depth_charts) == 0:
        return out
    subset = depth_charts[depth_charts["position"] == position]
    # normalise_depth_charts() emits "depth"; a raw legacy frame still calls it
    # "depth_team". Sorting on a missing column would raise, so pick whichever
    # is present and fall back to file order if neither is.
    rank = next((c for c in ("depth", "depth_team") if c in subset.columns), None)
    if rank is not None:
        subset = subset.sort_values(rank)
    for row in subset.to_dict("records"):
        team = row.get("team")
        if not team:
            continue
        key = (int(row.get("season") or 0), int(row.get("week") or 0), team)
        name = normalize_name(row.get("full_name"))
        if name and name not in out.setdefault(key, []):
            out[key].append(name)
    return out


def build_adjustments(
    injuries_by_team: dict[str, list[dict]],
    *,
    qb_values: dict[str, float] | None = None,
    depth: dict[str, list[str]] | None = None,
    qb_priced: set[str] | None = None,
) -> dict[str, TeamAvailability]:
    """``qb_priced`` names teams whose replacement starter is already a model
    feature; see :func:`team_adjustment`."""
    priced = qb_priced or set()
    return {
        team: team_adjustment(
            team, rows, qb_values=qb_values, depth=depth,
            qb_priced=team in priced,
        )
        for team, rows in injuries_by_team.items()
    }
