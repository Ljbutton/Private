"""Grade finished games against what we said beforehand.

The honest test of a model is not "did the pick win" but closing-line value: did
we take a number better than the one the market settled on?  ATS records over a
season are mostly noise (a 55% season on 250 bets is well within chance), while
consistently positive CLV is very hard to produce by luck.  Both are recorded,
with CLV treated as the primary signal.
"""

from __future__ import annotations

import math

from .. import db
from ..picks.edges import MIN_SPREAD_EDGE, MIN_TOTAL_EDGE
from ..util import now_iso

PUSH = "push"


def _first_and_last(rows: list[dict], field: str) -> tuple[dict | None, dict | None]:
    """Earliest and latest observation of a market.

    Both are None unless there are at least two *distinct* snapshots: with a
    single observation the opening and closing numbers are the same row, and
    reporting a closing-line value of exactly zero would be a fiction rather
    than a measurement.
    """
    usable = [r for r in rows if r.get(field) is not None]
    if len(usable) < 2:
        return (usable[0], None) if usable else (None, None)
    return usable[0], usable[-1]


def grade_game(game: dict) -> dict | None:
    """Grade one completed game. Returns the row written to ``graded``."""
    game_id = str(game["game_id"])
    if game.get("home_score") is None or game.get("away_score") is None:
        return None
    actual_margin = float(game["home_score"]) - float(game["away_score"])
    actual_total = float(game["home_score"]) + float(game["away_score"])

    predictions = db.query(
        "SELECT * FROM predictions WHERE game_id = ? ORDER BY captured_at", (game_id,)
    )
    consensus = db.query(
        "SELECT * FROM consensus WHERE game_id = ? ORDER BY captured_at", (game_id,)
    )
    if not predictions:
        return None

    # "Our bet" is the first prediction that had a line to bet into; the closing
    # number is the last one observed before kickoff.
    first_spread_row, close_spread_row = _first_and_last(consensus, "spread_home")
    first_total_row, close_total_row = _first_and_last(consensus, "total_points")
    first_spread = first_spread_row["spread_home"] if first_spread_row else None
    close_spread = close_spread_row["spread_home"] if close_spread_row else None
    first_total = first_total_row["total_points"] if first_total_row else None
    close_total = close_total_row["total_points"] if close_total_row else None

    entry = next((p for p in predictions if p.get("market_spread") is not None), predictions[0])
    pred_margin = float(entry["margin_home"])
    pred_total = float(entry["total_points"])
    pred_prob = float(entry["home_win_prob"])
    bet_spread = entry.get("market_spread")
    bet_total = entry.get("market_total")

    # ---- spread
    ats_pick = "none"
    ats_result = "none"
    clv_spread = None
    if bet_spread is not None:
        edge = pred_margin + float(bet_spread)
        if abs(edge) >= MIN_SPREAD_EDGE:
            ats_pick = "home" if edge > 0 else "away"
            margin_vs_line = actual_margin + float(bet_spread)
            if abs(margin_vs_line) < 1e-9:
                ats_result = PUSH
            elif (margin_vs_line > 0) == (ats_pick == "home"):
                ats_result = "win"
            else:
                ats_result = "loss"
            if close_spread is not None:
                # Points gained versus the closing number, from our side.
                delta = float(bet_spread) - float(close_spread)
                clv_spread = delta if ats_pick == "home" else -delta

    # ---- total
    total_pick = "none"
    total_result = "none"
    clv_total = None
    if bet_total is not None:
        edge = pred_total - float(bet_total)
        if abs(edge) >= MIN_TOTAL_EDGE:
            total_pick = "over" if edge > 0 else "under"
            diff = actual_total - float(bet_total)
            if abs(diff) < 1e-9:
                total_result = PUSH
            elif (diff > 0) == (total_pick == "over"):
                total_result = "win"
            else:
                total_result = "loss"
            if close_total is not None:
                # An over bet gains when the total closes higher than we took it.
                delta = float(close_total) - float(bet_total)
                clv_total = delta if total_pick == "over" else -delta

    su_correct = None
    brier = None
    log_loss = None
    if actual_margin != 0:
        home_won = 1.0 if actual_margin > 0 else 0.0
        su_correct = int((pred_prob > 0.5) == (home_won == 1.0))
        p = min(max(pred_prob, 1e-6), 1 - 1e-6)
        brier = (p - home_won) ** 2
        log_loss = -(home_won * math.log(p) + (1 - home_won) * math.log(1 - p))

    return {
        "game_id": game_id,
        "season": int(game["season"]),
        "week": int(game["week"]),
        "graded_at": now_iso(),
        "actual_margin": actual_margin,
        "actual_total": actual_total,
        "first_spread": first_spread,
        "close_spread": close_spread,
        "first_total": first_total,
        "close_total": close_total,
        "pred_margin": pred_margin,
        "pred_total": pred_total,
        "pred_home_prob": pred_prob,
        "ats_pick": ats_pick,
        "ats_result": ats_result,
        "total_pick": total_pick,
        "total_result": total_result,
        "su_correct": su_correct,
        "clv_spread": clv_spread,
        "clv_total": clv_total,
        "brier": brier,
        "log_loss": log_loss,
    }


def grade_completed_games(season: int | None = None, *, regrade: bool = False) -> int:
    """Grade every final game that has a prediction on file. Returns the count."""
    where = ["g.status = 'final'"]
    params: list = []
    if season is not None:
        where.append("g.season = ?")
        params.append(season)
    if not regrade:
        where.append("gr.game_id IS NULL")

    games = db.query(
        "SELECT g.* FROM games g LEFT JOIN graded gr ON gr.game_id = g.game_id "
        f"WHERE {' AND '.join(where)} ORDER BY g.kickoff",
        params,
    )

    rows = [r for r in (grade_game(g) for g in games) if r]
    if not rows:
        return 0
    columns = list(rows[0].keys())
    db.executemany(
        f"INSERT OR REPLACE INTO graded({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        [[r[c] for c in columns] for r in rows],
    )
    return len(rows)
