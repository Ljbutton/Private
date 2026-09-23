"""Who takes the first snap this week, rather than who took it last week.

The feature builder reads the starting quarterback from the schedule feed, which
records a starter only *after* a game is played. For a game that has not kicked
off there is no such field, so it falls back to whoever started last week. That
is wrong in exactly the case that moves a line most — a starter ruled out on
Wednesday — and it is wrong in a way that compounds: the model prices the game
with the injured starter's rating *and* reports `qb_change = 0`, so nothing
downstream knows the projection is stale.

Two sources, kept separate because they fail differently:

* **Unavailability** comes from the injury report. It is explicit and
  machine-readable, and a team that rules a player out has told you so.
* **The replacement** comes from the published depth chart. Inferring it from
  who has started before fails precisely for the team whose No. 2 has never
  started, which is the case you need it for.

Free-text news is deliberately *not* used to override the starter. The news
classifier reports a category, a team and a position, but never a player name,
so it cannot say which quarterback a headline means — and a wrong identity here
does not degrade the projection gracefully, it prices the wrong player.
"""

from __future__ import annotations

from dataclasses import dataclass

from .availability import normalize_name, status_cost

# How unavailable a player must be before we assume he does not start. "Out",
# "doubtful", "injured reserve" and "suspended" clear this; "questionable" does
# not. A questionable starter usually plays, and when he plays hurt the partial
# cost the availability adjustment already charges is the better description —
# swapping him out entirely would overstate a coin flip as a certainty.
RULED_OUT = 0.75


@dataclass(frozen=True)
class ExpectedStarter:
    """Who we think starts, and whether that differs from the depth chart."""

    team: str
    player: str                  # normalised name key, e.g. "P.MAHOMES"
    nominal: str | None          # who the depth chart lists first
    changed: bool                # the listed starter is not the expected one
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "team": self.team,
            "player": self.player,
            "nominal": self.nominal,
            "changed": self.changed,
            "reason": self.reason,
        }


def _ruled_out(injuries: list[dict], threshold: float) -> dict[str, str]:
    """Normalised name -> the status that ruled him out."""
    out: dict[str, str] = {}
    for item in injuries or []:
        key = item.get("player") or normalize_name(item.get("player_name"))
        if not key:
            continue
        status = item.get("status")
        if status_cost(status) >= threshold:
            out[key] = str(status or "out")
    return out


def expected_starters(
    depth: dict[str, list[str]],
    injuries_by_team: dict[str, list[dict]],
    *,
    threshold: float = RULED_OUT,
) -> dict[str, ExpectedStarter]:
    """team -> the quarterback we expect to start, given who has been ruled out.

    ``depth`` is each team's passers in depth order, by normalised name, and
    ``injuries_by_team`` is the current report in the shape
    :func:`nflpicker.availability.build_adjustments` consumes.
    """
    out: dict[str, ExpectedStarter] = {}
    for team, players in (depth or {}).items():
        ordered = [p for p in players if p]
        if not ordered:
            continue
        nominal = ordered[0]
        unavailable = _ruled_out(injuries_by_team.get(team, []), threshold)

        starter = next((p for p in ordered if p not in unavailable), None)
        if starter is None:
            # Every listed passer is out. Someone still starts, and we have no
            # basis for guessing who, so we keep the nominal starter and say so
            # rather than silently picking the least-injured name.
            out[team] = ExpectedStarter(
                team=team, player=nominal, nominal=nominal, changed=False,
                reason="every listed quarterback is ruled out",
            )
            continue

        changed = starter != nominal
        out[team] = ExpectedStarter(
            team=team, player=starter, nominal=nominal, changed=changed,
            reason=f"{nominal} is {unavailable.get(nominal, 'out')}" if changed else "",
        )
    return out


def apply_to_games(
    games: list[dict],
    starters: dict[str, ExpectedStarter],
    qb_ids: dict[str, str],
) -> int:
    """Fill in the starting quarterback for games that have not been played.

    Only unplayed games are touched. A final game already carries the starter
    who actually played, and overwriting that with today's injury report would
    rewrite history with information from the future — the exact leak the
    feature builder is otherwise careful to avoid.

    Returns the number of sides filled in, so a caller can log that the feature
    reached inference rather than assuming it did.
    """
    filled = 0
    for game in games:
        if str(game.get("status") or "").lower() == "final":
            continue
        for side in ("home", "away"):
            team = game.get(side)
            expected = starters.get(team) if team else None
            if expected is None:
                continue
            # Never overwrite an identity the feed already supplied.
            if game.get(f"{side}_qb_id"):
                continue
            qb_id = qb_ids.get(expected.player)
            if not qb_id:
                # A passer with no history under any id. Leaving the field empty
                # would fall back to last week's starter, which is the bug; the
                # name keeps him distinct so `qb_change` still fires, and the
                # rating tracker treats an unknown id as a debut start.
                qb_id = f"name:{expected.player}"
            game[f"{side}_qb_id"] = qb_id
            game[f"{side}_qb_name"] = expected.player
            filled += 1
    return filled
