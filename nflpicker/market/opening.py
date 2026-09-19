"""Opening lines, and the one test the model has not yet taken.

Everything measured so far compares our number to the **closing** line, and the
answer is that we do not beat it. But the closing line is the end of a week-long
process: lines open on Sunday night, and they are at their softest before the
market has chewed on them.

That makes a sharper, falsifiable question available: **does our number predict
which way the line moves?** If the model has any information the opening market
lacks, the line should drift toward us more often than away. That is measurable
from our own snapshots, requires no opinion about the final result, and cannot
be faked by a lucky week — line movement is far less noisy than game outcomes,
so it needs a fraction of the sample that an ATS record does.

The two measures here are related but different:

* **Movement agreement** — did the line move toward the side we favoured? Pure
  signal about the model, independent of results.
* **Closing-line value** — how many points better than the close was the number
  we could have taken? This is the one that translates into money.

Both need the app to have been *running* before kickoff. Neither can be
backfilled: nobody publishes a history of intraday NFL line movement, and the
snapshots are the record.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import db

# A line has to move by at least this much before the direction means anything;
# a half-point wobble between books is noise, not the market revising.
MIN_MOVE = 0.5

# Our disagreement has to be at least this large to count as a lean.
MIN_LEAN = 0.5


@dataclass
class MovementCase:
    """One game where we had a view before the market finished forming one."""

    game_id: str
    home: str
    away: str
    kickoff: str | None
    open_spread: float
    close_spread: float
    first_seen: str
    model_margin: float
    lean: float          # our margin vs the market's, at the time we saw it
    movement: float      # how the market's implied margin moved afterwards
    agreed: bool         # did it move toward our side?
    clv: float | None    # points gained against the close, on our side

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id, "home": self.home, "away": self.away,
            "kickoff": self.kickoff,
            "open_spread": round(self.open_spread, 2),
            "close_spread": round(self.close_spread, 2),
            "first_seen": self.first_seen,
            "model_margin": round(self.model_margin, 2),
            "lean": round(self.lean, 2),
            "movement": round(self.movement, 2),
            "agreed": self.agreed,
            "clv": None if self.clv is None else round(self.clv, 2),
        }


def opening_and_closing(game_id: str) -> tuple[dict | None, dict | None]:
    """First and last consensus observations for a game.

    "Opening" here means the first line *we saw*, which is only the true opener
    if the app was running when the market posted it. That distinction is
    surfaced rather than hidden, via ``hours_before_kickoff``.
    """
    rows = db.query(
        "SELECT captured_at, spread_home, total_points FROM consensus "
        "WHERE game_id = ? AND spread_home IS NOT NULL ORDER BY captured_at",
        (game_id,),
    )
    if not rows:
        return None, None
    return rows[0], rows[-1]


def movement_cases(season: int | None = None, week: int | None = None) -> list[MovementCase]:
    """Every game where we recorded a view and the line subsequently moved."""
    where = ["c.spread_home IS NOT NULL"]
    params: list = []
    if season is not None:
        where.append("g.season = ?")
        params.append(season)
    if week is not None:
        where.append("g.week = ?")
        params.append(week)

    games = db.query(
        "SELECT DISTINCT g.game_id, g.home, g.away, g.kickoff FROM games g "
        f"JOIN consensus c ON c.game_id = g.game_id WHERE {' AND '.join(where)}",
        params,
    )

    cases: list[MovementCase] = []
    for game in games:
        opening, closing = opening_and_closing(game["game_id"])
        if not opening or not closing or opening["captured_at"] == closing["captured_at"]:
            continue

        # The earliest prediction we made while the opening line still stood.
        prediction = db.query_one(
            "SELECT captured_at, margin_home FROM predictions "
            "WHERE game_id = ? AND captured_at >= ? ORDER BY captured_at LIMIT 1",
            (game["game_id"], opening["captured_at"]),
        )
        if not prediction:
            continue

        open_spread = float(opening["spread_home"])
        close_spread = float(closing["spread_home"])
        model = float(prediction["margin_home"])

        # Market implied margin is the negation of the posted home line.
        lean = model - (-open_spread)
        movement = (-close_spread) - (-open_spread)
        if abs(lean) < MIN_LEAN or abs(movement) < MIN_MOVE:
            continue

        agreed = (lean > 0) == (movement > 0)
        # CLV on the side we leaned: taking the open, how much better was it?
        clv = (open_spread - close_spread) if lean > 0 else (close_spread - open_spread)

        cases.append(
            MovementCase(
                game_id=game["game_id"], home=game["home"], away=game["away"],
                kickoff=game["kickoff"],
                open_spread=open_spread, close_spread=close_spread,
                first_seen=prediction["captured_at"], model_margin=model,
                lean=lean, movement=movement, agreed=agreed, clv=clv,
            )
        )
    return cases


def movement_report(season: int | None = None) -> dict:
    """Does the line move toward us? The headline test of the model's edge."""
    from ..backtest.teasers import wilson_interval

    cases = movement_cases(season)
    if not cases:
        return {
            "n": 0,
            "note": (
                "No line movement recorded yet. This measure needs the app to have been "
                "running before kickoff, with at least two market observations per game — "
                "it cannot be backfilled, because nobody publishes a history of intraday "
                "NFL line movement."
            ),
        }

    agreed = sum(1 for c in cases if c.agreed)
    n = len(cases)
    rate = agreed / n
    low, high = wilson_interval(agreed, n)
    clvs = [c.clv for c in cases if c.clv is not None]

    # Bucket by how far ahead of kickoff we formed the view: if the model has an
    # edge anywhere it should be largest early, before the market has worked.
    from ..util import hours_between

    buckets: dict[str, list[MovementCase]] = {"> 72h": [], "24-72h": [], "< 24h": []}
    for case in cases:
        hours = hours_between(case.first_seen, case.kickoff)
        key = "> 72h" if (hours or 0) > 72 else ("24-72h" if (hours or 0) > 24 else "< 24h")
        buckets[key].append(case)

    return {
        "n": n,
        "agreed": agreed,
        "agreement_rate": round(rate, 4),
        "ci_low": round(low, 4),
        "ci_high": round(high, 4),
        # 50% is a coin flip: the line was always going to move one way or other.
        "beats_coin_flip": low > 0.5,
        "avg_clv": round(sum(clvs) / len(clvs), 3) if clvs else None,
        "positive_clv_rate": (
            round(sum(1 for c in clvs if c > 0) / len(clvs), 4) if clvs else None
        ),
        "avg_move": round(sum(abs(c.movement) for c in cases) / n, 2),
        "by_lead_time": [
            {
                "window": window,
                "n": len(rows),
                "agreement_rate": (
                    round(sum(1 for c in rows if c.agreed) / len(rows), 4) if rows else None
                ),
                "avg_clv": (
                    round(sum(c.clv for c in rows if c.clv is not None) / len(rows), 3)
                    if rows else None
                ),
            }
            for window, rows in buckets.items()
        ],
        "biggest_moves": [
            c.to_dict() for c in sorted(cases, key=lambda c: -abs(c.movement))[:15]
        ],
    }


def coverage() -> dict:
    """How much of the market's own history the app has actually witnessed.

    Without this the movement numbers look like a verdict when they may only be
    a small and unrepresentative slice.
    """
    row = db.query_one(
        "SELECT COUNT(DISTINCT game_id) AS games, COUNT(*) AS snapshots FROM consensus"
    )
    multi = db.query_one(
        "SELECT COUNT(*) AS n FROM (SELECT game_id FROM consensus "
        "GROUP BY game_id HAVING COUNT(*) > 1)"
    )
    earliest = db.query_one("SELECT MIN(captured_at) AS t FROM consensus")
    return {
        "games_with_any_line": (row or {}).get("games", 0),
        "games_with_movement": (multi or {}).get("n", 0),
        "snapshots": (row or {}).get("snapshots", 0),
        "watching_since": (earliest or {}).get("t"),
    }
