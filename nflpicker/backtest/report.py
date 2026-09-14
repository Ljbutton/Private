"""Aggregate performance: ATS record, ROI, CLV, calibration."""

from __future__ import annotations

from .. import db

# Standard -110 juice: you risk 110 to win 100.
JUICE_STAKE = 1.10


def _roi(wins: int, losses: int) -> float | None:
    """Return on amount risked at -110."""
    risked = (wins + losses) * JUICE_STAKE
    if risked <= 0:
        return None
    return (wins * 1.0 - losses * JUICE_STAKE) / risked


def performance_report(season: int | None = None) -> dict:
    where = "WHERE season = ?" if season is not None else ""
    params = [season] if season is not None else []
    rows = db.query(f"SELECT * FROM graded {where} ORDER BY season, week", params)
    if not rows:
        return {"n_games": 0, "note": "No graded games yet — results appear once games finish."}

    ats = [r for r in rows if r["ats_result"] in {"win", "loss", "push"}]
    ats_wins = sum(1 for r in ats if r["ats_result"] == "win")
    ats_losses = sum(1 for r in ats if r["ats_result"] == "loss")
    ats_pushes = sum(1 for r in ats if r["ats_result"] == "push")

    totals = [r for r in rows if r["total_result"] in {"win", "loss", "push"}]
    tot_wins = sum(1 for r in totals if r["total_result"] == "win")
    tot_losses = sum(1 for r in totals if r["total_result"] == "loss")
    tot_pushes = sum(1 for r in totals if r["total_result"] == "push")

    clv_spread = [r["clv_spread"] for r in rows if r["clv_spread"] is not None]
    clv_total = [r["clv_total"] for r in rows if r["clv_total"] is not None]
    su = [r["su_correct"] for r in rows if r["su_correct"] is not None]
    brier = [r["brier"] for r in rows if r["brier"] is not None]
    log_loss = [r["log_loss"] for r in rows if r["log_loss"] is not None]

    margin_errors = [
        abs(r["pred_margin"] - r["actual_margin"])
        for r in rows if r["pred_margin"] is not None and r["actual_margin"] is not None
    ]
    # Prefer the closing number; fall back to the only observation we have, so
    # the market benchmark still appears for backfilled history.
    market_errors = []
    for r in rows:
        line = r["close_spread"] if r["close_spread"] is not None else r["first_spread"]
        if line is not None and r["actual_margin"] is not None:
            market_errors.append(abs(-line - r["actual_margin"]))

    backfilled = db.query_one(
        "SELECT COUNT(DISTINCT g.game_id) AS n FROM graded g JOIN predictions p "
        "ON p.game_id = g.game_id WHERE p.model_version LIKE '%:backfill'"
        + (" AND g.season = ?" if season is not None else ""),
        params,
    )
    return {
        "n_games": len(rows),
        "season": season,
        "backfilled": (backfilled or {}).get("n", 0),
        "backfill_note": (
            "Backfilled grades replay the model over games already played. Features are "
            "leak-free, but if the model was trained on these seasons the results are "
            "in-sample — trust the walk-forward numbers in the training report instead."
        ) if (backfilled or {}).get("n", 0) else None,
        "ats": {
            "wins": ats_wins, "losses": ats_losses, "pushes": ats_pushes,
            "rate": round(ats_wins / (ats_wins + ats_losses), 4) if (ats_wins + ats_losses) else None,
            "roi": round(_roi(ats_wins, ats_losses), 4) if (ats_wins + ats_losses) else None,
            "units": round(ats_wins - ats_losses * JUICE_STAKE, 2),
        },
        "totals": {
            "wins": tot_wins, "losses": tot_losses, "pushes": tot_pushes,
            "rate": round(tot_wins / (tot_wins + tot_losses), 4) if (tot_wins + tot_losses) else None,
            "roi": round(_roi(tot_wins, tot_losses), 4) if (tot_wins + tot_losses) else None,
            "units": round(tot_wins - tot_losses * JUICE_STAKE, 2),
        },
        "clv": {
            "spread_avg": round(sum(clv_spread) / len(clv_spread), 3) if clv_spread else None,
            "spread_n": len(clv_spread),
            "note": (
                "Closing-line value needs at least two market observations per game, so "
                "it only accumulates for games this app watched before kickoff."
            ) if not clv_spread else None,
            "spread_positive_rate": (
                round(sum(1 for c in clv_spread if c > 0) / len(clv_spread), 3)
                if clv_spread else None
            ),
            "total_avg": round(sum(clv_total) / len(clv_total), 3) if clv_total else None,
            "total_n": len(clv_total),
        },
        "straight_up": {
            "correct": sum(su), "n": len(su),
            "rate": round(sum(su) / len(su), 4) if su else None,
        },
        "calibration": {
            "brier": round(sum(brier) / len(brier), 4) if brier else None,
            "log_loss": round(sum(log_loss) / len(log_loss), 4) if log_loss else None,
            "buckets": calibration_buckets(rows),
        },
        "accuracy": {
            "margin_mae": round(sum(margin_errors) / len(margin_errors), 3) if margin_errors else None,
            "market_margin_mae": round(sum(market_errors) / len(market_errors), 3) if market_errors else None,
        },
        "weekly": weekly_series(rows),
    }


def calibration_buckets(rows: list[dict], n_buckets: int = 5) -> list[dict]:
    """Predicted probability versus observed frequency, for a calibration chart."""
    buckets: list[dict] = []
    for i in range(n_buckets):
        lo = 0.5 + i * (0.5 / n_buckets)
        hi = 0.5 + (i + 1) * (0.5 / n_buckets)
        picked = []
        for r in rows:
            prob = r.get("pred_home_prob")
            correct = r.get("su_correct")
            if prob is None or correct is None:
                continue
            confidence = max(prob, 1 - prob)
            if lo <= confidence < hi or (i == n_buckets - 1 and confidence >= hi):
                picked.append(correct)
        if picked:
            buckets.append(
                {
                    "range": f"{lo:.0%}-{hi:.0%}",
                    "predicted": round((lo + hi) / 2, 3),
                    "observed": round(sum(picked) / len(picked), 3),
                    "n": len(picked),
                }
            )
    return buckets


def weekly_series(rows: list[dict]) -> list[dict]:
    """Per-week record, so the dashboard can chart the run over time."""
    by_week: dict[tuple[int, int], dict] = {}
    for r in rows:
        key = (r["season"], r["week"])
        bucket = by_week.setdefault(
            key, {"season": r["season"], "week": r["week"], "ats_wins": 0,
                  "ats_losses": 0, "su_correct": 0, "n": 0, "clv": []}
        )
        bucket["n"] += 1
        if r["ats_result"] == "win":
            bucket["ats_wins"] += 1
        elif r["ats_result"] == "loss":
            bucket["ats_losses"] += 1
        if r["su_correct"]:
            bucket["su_correct"] += 1
        if r["clv_spread"] is not None:
            bucket["clv"].append(r["clv_spread"])

    series = []
    cumulative = 0.0
    for key in sorted(by_week):
        bucket = by_week[key]
        cumulative += bucket["ats_wins"] - bucket["ats_losses"] * JUICE_STAKE
        series.append(
            {
                "season": bucket["season"],
                "week": bucket["week"],
                "ats_wins": bucket["ats_wins"],
                "ats_losses": bucket["ats_losses"],
                "su_correct": bucket["su_correct"],
                "n": bucket["n"],
                "clv": round(sum(bucket["clv"]) / len(bucket["clv"]), 3) if bucket["clv"] else None,
                "cumulative_units": round(cumulative, 2),
            }
        )
    return series
