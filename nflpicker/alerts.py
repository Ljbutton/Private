"""Things worth noticing while the app is running.

In-app only, on purpose. The app already sits open on a desk; what it lacked
was any way to say "something changed" without you re-reading sixteen rows to
find it.

Two rules keep this from becoming noise:

* **Every alert is about a change, not a state.** "Denver is favoured" is the
  board's job. "Denver's starter was ruled out" is an alert.
* **Each distinct event fires once.** Recompute runs every few minutes, so
  anything keyed on current state would re-raise the same alert until kickoff.
  A fingerprint per event makes the insert idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db
from .util import now_iso

# Crossing one of these is a different game, not a slightly different one.
KEY_NUMBERS = (3.0, 7.0)

# A line has to move at least this far before it is worth a line of your
# attention; below it the board is the right place to notice.
MIN_MOVE = 1.0


@dataclass(frozen=True)
class Alert:
    kind: str
    severity: str
    title: str
    detail: str = ""
    game_id: str | None = None
    fingerprint: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "severity": self.severity, "title": self.title,
            "detail": self.detail, "game_id": self.game_id,
            "fingerprint": self.fingerprint,
        }


def record(alerts: list[Alert]) -> int:
    """Store alerts, ignoring ones already raised. Returns how many are new."""
    if not alerts:
        return 0
    stamp = now_iso()
    before = db.query_one("SELECT COUNT(*) AS n FROM alerts")
    db.executemany(
        "INSERT OR IGNORE INTO alerts"
        "(created_at, kind, severity, game_id, title, detail, fingerprint) "
        "VALUES(?,?,?,?,?,?,?)",
        [[stamp, a.kind, a.severity, a.game_id, a.title, a.detail, a.fingerprint]
         for a in alerts],
    )
    after = db.query_one("SELECT COUNT(*) AS n FROM alerts")
    return int((after or {}).get("n", 0)) - int((before or {}).get("n", 0))


def recent(limit: int = 40, unseen_only: bool = False) -> list[dict]:
    where = " WHERE seen = 0" if unseen_only else ""
    return db.query(
        f"SELECT * FROM alerts{where} ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    )


def mark_seen(ids: list[int] | None = None) -> None:
    if ids:
        placeholders = ",".join("?" for _ in ids)
        db.execute(f"UPDATE alerts SET seen = 1 WHERE id IN ({placeholders})",  # noqa: S608
                   tuple(ids))
    else:
        db.execute("UPDATE alerts SET seen = 1 WHERE seen = 0")


def _crossed(opening: float, current: float) -> float | None:
    """The key number a line moved across, if any.

    Both sides are compared as the home team's line, so a move from -3.5 to -2.5
    crosses 3 and a move from -2.5 to -3.5 crosses it the other way. Only a
    genuine crossing counts: landing exactly on the number is not passing it.
    """
    low, high = sorted((abs(opening), abs(current)))
    for key in KEY_NUMBERS:
        if low < key < high:
            return key
    return None


def from_games(games: list[dict]) -> list[Alert]:
    """Derive this refresh's alerts from the game cards the dashboard renders.

    Built from the same payload the board shows, rather than from a second set
    of queries, so an alert can never describe something the screen disagrees
    with.
    """
    out: list[Alert] = []
    for game in games:
        if str(game.get("status") or "").lower() == "final":
            continue
        gid = game.get("game_id")
        matchup = f"{game.get('away')} @ {game.get('home')}"

        # A starter ruled out, which is the single biggest thing that can
        # change between one look at the board and the next.
        for side in ("home", "away"):
            availability = (game.get("availability") or {}).get(side) or {}
            if not availability.get("qb_change"):
                continue
            team = game.get(side)
            out.append(Alert(
                kind="starter", severity="urgent",
                title=f"{team}: quarterback change",
                detail=f"{matchup} — the listed starter is ruled out; "
                       "the projection is using the next man on the depth chart.",
                game_id=gid,
                fingerprint=f"starter:{gid}:{team}",
            ))

        movement = game.get("movement") or {}
        opening, current = movement.get("spread_open"), movement.get("spread_now")
        if opening is not None and current is not None:
            key = _crossed(float(opening), float(current))
            if key is not None:
                out.append(Alert(
                    kind="key_number", severity="notable",
                    title=f"{matchup}: line crossed {key:g}",
                    detail=f"Opened {float(opening):+.1f}, now {float(current):+.1f}. "
                           "More NFL games land on 3 and 7 than on any other margin, "
                           "so crossing one changes the bet more than the half-point "
                           "suggests.",
                    game_id=gid,
                    # Keyed on the number crossed, not the current line, so the
                    # same crossing does not re-fire as the line keeps drifting.
                    fingerprint=f"key:{gid}:{key:g}",
                ))

        if movement.get("steam"):
            out.append(Alert(
                kind="steam", severity="notable",
                title=f"{matchup}: steam move",
                detail="Several books moved together and quickly, which usually "
                       "means money rather than news.",
                game_id=gid,
                fingerprint=f"steam:{gid}:{movement.get('spread_now')}",
            ))

        # Where the model disagrees with the *opening* line by enough to be
        # worth a look. See nflpicker/market/opening.py for why the opener is
        # the more interesting number to disagree with.
        opener_edge = movement.get("opener_edge")
        if opener_edge is not None and abs(float(opener_edge)) >= 2.0:
            out.append(Alert(
                kind="edge", severity="info",
                title=f"{matchup}: {float(opener_edge):+.1f} against the opener",
                detail="Our number disagrees with the line as it opened. That is "
                       "the softer of the two prices; the closing line is not.",
                game_id=gid,
                fingerprint=f"opener:{gid}:{round(float(opener_edge))}",
            ))

    return out
