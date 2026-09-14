"""Line movement: how the market's opinion changed over time.

Movement is the market's own revision history.  Steam (a fast move across every
book at once) usually means sharp money or news; one book drifting alone is
usually noise.  Both are surfaced so the dashboard can distinguish them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import db


@dataclass
class MovementPoint:
    captured_at: str
    value: float
    source: str  # book key or "consensus"


def consensus_series(game_id: str, market: str = "spread") -> list[dict]:
    """Consensus history for one game, oldest first."""
    column = {
        "spread": "spread_home",
        "total": "total_points",
        "moneyline": "home_win_prob",
    }.get(market, "spread_home")
    # `column` is chosen from a fixed whitelist above, never from user input.
    sql = (
        f"SELECT captured_at, {column} AS value, n_books FROM consensus "
        f"WHERE game_id = ? AND {column} IS NOT NULL ORDER BY captured_at"
    )
    rows = db.query(sql, (game_id,))
    return [r for r in rows if r.get("value") is not None]


def book_series(game_id: str, market: str = "spread") -> dict[str, list[dict]]:
    """Per-book history, so the chart can show books diverging."""
    rows = db.query(
        "SELECT book, captured_at, home_point, away_point, home_price, away_price "
        "FROM odds_snapshots WHERE game_id = ? AND market = ? ORDER BY captured_at",
        (game_id, market),
    )
    out: dict[str, list[dict]] = {}
    for row in rows:
        if row["home_point"] is None and row["home_price"] is None:
            continue
        out.setdefault(row["book"], []).append(
            {
                "captured_at": row["captured_at"],
                "value": row["home_point"] if row["home_point"] is not None else row["home_price"],
                "home_price": row["home_price"],
                "away_price": row["away_price"],
            }
        )
    return out


def summarise(game_id: str, market: str = "spread") -> dict:
    """Open, current, net move, and whether the move looks like steam."""
    series = consensus_series(game_id, market)
    if not series:
        return {"points": [], "open": None, "current": None, "move": None, "steam": False}
    opening = float(series[0]["value"])
    current = float(series[-1]["value"])
    move = current - opening

    # Steam: most of the total move happened in a single interval.
    steam = False
    if len(series) >= 3 and abs(move) >= 1.0:
        steps = [
            abs(float(series[i + 1]["value"]) - float(series[i]["value"]))
            for i in range(len(series) - 1)
        ]
        steam = max(steps) >= 0.7 * abs(move) and max(steps) >= 1.0

    return {
        "points": series,
        "open": opening,
        "current": current,
        "move": round(move, 2),
        "steam": steam,
        "n_points": len(series),
    }


def reverse_line_move(game_id: str) -> bool:
    """True when the line moved toward the side the model already liked.

    Not a betting signal on its own, but useful context next to an edge: a line
    moving *away* from your number usually means the market saw something first.
    """
    spread = summarise(game_id, "spread")
    if spread["move"] is None:
        return False
    latest = db.query_one(
        "SELECT spread_edge FROM predictions WHERE game_id = ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (game_id,),
    )
    if not latest or latest.get("spread_edge") is None:
        return False
    return (spread["move"] > 0) == (float(latest["spread_edge"]) > 0)
