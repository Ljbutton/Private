"""Command line entry point: ``nflpicker <command>``."""

from __future__ import annotations

import argparse
import json
import sys

from . import db
from .config import get_config


def _print_table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        print("(nothing to show)")
        return
    widths = {c: max(len(c), *(len(_fmt(r.get(c))) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  ".join(_fmt(row.get(c)).ljust(widths[c]) for c in columns))


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


# ------------------------------------------------------------------ commands

def cmd_serve(args) -> int:
    import uvicorn

    cfg = get_config()
    host = args.host or cfg.host
    port = args.port or cfg.port
    mode = "demo (synthetic data)" if cfg.demo else "live"
    print(f"nflpicker serving on http://{host}:{port}  [{mode}]")
    if not cfg.has_odds_key and not cfg.demo:
        print("  note: no ODDS_API_KEY set — using ESPN's single consensus line only.")
    uvicorn.run(
        "nflpicker.api:create_app",
        factory=True,
        host=host,
        port=port,
        log_level=args.log_level,
        reload=args.reload,
    )
    return 0


def cmd_desktop(args) -> int:
    """Run the dashboard in a native window rather than a browser."""
    from .desktop import run

    return run(width=args.width, height=args.height, debug=args.debug)


def cmd_refresh(args) -> int:
    from .pipeline import Pipeline

    pipeline = Pipeline()
    stages = args.stages.split(",") if args.stages else None
    result = pipeline.refresh(stages, force_odds=args.force)
    for stage, info in result.stages.items():
        mark = "ok " if info["ok"] else "FAIL"
        print(f"[{mark}] {stage:<10} {info['detail']}")
    return 0 if all(s["ok"] for s in result.stages.values()) else 1


def cmd_train(args) -> int:
    import warnings

    from .ml.dataset import from_database, from_nflverse
    from .ml.train import train

    warnings.filterwarnings("ignore")
    cfg = get_config()

    source = ("db" if cfg.demo else "nflverse") if args.source == "auto" else args.source

    if source == "nflverse":
        print("downloading nflverse game history…")
        frame = from_nflverse(args.since, with_epa=not args.no_epa,
                              progress=lambda m: print(m, flush=True))
    else:
        frame = from_database(progress=print)

    if frame.empty:
        print("no usable games — run `nflpicker refresh` first.")
        return 1

    print(f"building features: {frame.shape[0]} games x {frame.shape[1]} columns")
    print("walk-forward evaluation (this is the slow part)…")
    report = train(frame, evaluate_first=not args.fast)
    payload = report.to_dict()
    print(json.dumps(payload, indent=2))

    blind = payload["blind"]
    if blind.get("margin_mae"):
        market = blind.get("market_margin_mae")
        print(f"\nmargin MAE: model {blind['margin_mae']:.3f}", end="")
        if market:
            verdict = "better than" if blind["margin_mae"] < market else "worse than"
            print(f" vs market {market:.3f}  ({verdict} the closing line)")
        else:
            print()
        if blind.get("ats_rate"):
            print(f"ATS: {blind['ats_wins']}/{blind['ats_picks']} = {blind['ats_rate']:.1%} "
                  "(52.4% is break-even at -110)")
    for note in payload.get("notes", []):
        print(f"note: {note}")
    return 0


def cmd_backtest(args) -> int:
    from .backtest.grade import grade_completed_games
    from .backtest.report import performance_report
    from .pipeline import Pipeline

    if args.backfill:
        filled = Pipeline().backfill_predictions(args.season)
        print(f"backfilled {filled} historical predictions (in-sample if trained on them)")
    graded = grade_completed_games(args.season, regrade=args.regrade or args.backfill)
    print(f"graded {graded} games")
    print(json.dumps(performance_report(args.season), indent=2))
    return 0


def cmd_starters(args) -> int:
    """Does resolving the announced starter beat assuming last week's?"""
    import warnings

    warnings.filterwarnings("ignore")
    from .backtest.starters import measure

    seasons = list(range(args.since, args.until + 1)) if args.until else None
    print("replaying injury reports and depth charts…", flush=True)
    result = measure(seasons=seasons, progress=lambda m: print(m, flush=True))
    payload = result.to_dict()
    print(json.dumps(payload, indent=2))

    if not payload["n"]:
        print("\nno team-games could be scored — check the seasons requested.")
        return 1

    print(f"\nnaming the starter correctly, {payload['n']} team-games:")
    print(f"  last week's starter  {payload['last_week_rate']:.1%}")
    print(f"  announced starter    {payload['announced_rate']:.1%}")
    if payload["disagreed"]:
        print(f"\nthe two rules disagreed on {payload['disagreed']} of them, "
              "which is the only place the feature can change anything:")
        print(f"  last week's starter  {payload['last_week_rate_when_disagreed']:.1%}")
        print(f"  announced starter    {payload['announced_rate_when_disagreed']:.1%}")
    return 0


def cmd_teasers(args) -> int:
    """Backtest 6-point teasers through the key numbers."""
    import warnings

    warnings.filterwarnings("ignore")
    from .backtest.teasers import (
        by_era,
        describe,
        key_number_frequency,
        price_sensitivity,
        sweep,
        wong_windows,
    )
    from .sources.nflverse import NflverseSource

    print("downloading nflverse game history…")
    games = NflverseSource().games()
    usable = games.dropna(subset=["spread_line", "result"])
    print(f"{len(usable):,} games with a closing line and a result "
          f"({int(games.season.min())}-{int(games.season.max())})\n")

    print("=== Wong windows: teasing through 3 and 7 ===")
    for result in wong_windows(games, points=args.points):
        print("  " + describe(result))

    print("\n=== has it been priced away? ===")
    for result in by_era(games, points=args.points, split=args.split):
        print("  " + describe(result))

    print("\n=== the same bet at prices books actually offer ===")
    print("  (most books now price a 2-team 6-point teaser at -120 or worse)")
    for row in price_sensitivity(games, points=args.points):
        verdict = "playable" if row["roi_2leg"] > 0 else "not playable"
        print(f"  {row['label']:>5}: need {row['break_even_2leg']:.1%}, "
              f"got {row['win_rate']:.1%} → ROI {row['roi_2leg']:+.1%}  ({verdict})")

    if args.sweep:
        print("\n=== every window, not just the ones the theory names ===")
        for result in sorted(sweep(games, points=args.points), key=lambda r: -(r.edge or -1)):
            star = " *" if result.significant else ""
            print(f"  {result.label:>14}: {result.win_rate:.1%} "
                  f"[{result.ci_low:.1%}-{result.ci_high:.1%}] n={result.n:<5} "
                  f"edge {result.edge:+.1%}{star}")

    print("\n=== why it could work at all: where NFL margins land ===")
    for row in key_number_frequency(games):
        if row["share"] >= 0.04:
            bar = "#" * int(row["share"] * 120)
            print(f"  {row['margin']:>2}: {row['share']:>5.1%} {bar}")
    return 0


# Printed above anything that names a stake. The app's own measured record is
# that it does not beat the closing line, so a table headed "Best bets" without
# this is making a claim its own backtest contradicts.
WAGER_NOTICE = (
    "Not betting advice. Model output only. This model does not beat the "
    "closing line — about 51% against the spread, where 52.4% is break-even —\n"
    "so an edge here is inside the noise more often than not. "
    "Never stake what you cannot afford to lose. US helpline: 1-800-GAMBLER.\n"
)


def cmd_picks(args) -> int:
    from .pipeline import Pipeline

    pipeline = Pipeline()
    season = args.season or pipeline.season()
    week = args.week or pipeline.current_week(season)
    from .api import latest_pick

    if args.contest in ("all", "ats"):
        payload = latest_pick("ats", season, week) or {}
        print(f"\n=== Best bets — {season} week {week} ===")
        print(WAGER_NOTICE)
        _print_table(
            payload.get("edges", [])[:12],
            ["market", "selection", "book", "price", "win_prob", "expected_value",
             "kelly", "confidence"],
        )
    if args.contest in ("all", "pickem"):
        payload = latest_pick("pickem", season, week) or {}
        board = payload.get(args.mode) or payload.get("ev") or {}
        print(f"\n=== Pick'em ({board.get('mode', '?')}) — expected "
              f"{board.get('expected_points', 0):.1f} of {board.get('max_points', 0)} pts ===")
        _print_table(board.get("picks", []), ["confidence", "pick", "opponent", "win_prob", "edge"])
    if args.contest in ("all", "survivor"):
        payload = latest_pick("survivor", season, week) or {}
        rec = payload.get("recommendation")
        print("\n=== Survivor ===")
        if rec:
            print(f"Take {rec['pick' if 'pick' in rec else 'team']} vs {rec['opponent']} "
                  f"({rec['win_prob']:.1%}) — path survival "
                  f"{payload.get('survival_prob', 0):.1%} over {payload.get('horizon')} weeks")
            _print_table(payload.get("path", []), ["week", "team", "opponent", "win_prob"])
            if payload.get("alternatives"):
                print("\nIf you deviate this week:")
                _print_table(payload["alternatives"],
                             ["team", "opponent", "win_prob", "path_survival", "cost"])
        else:
            print(payload.get("note") or "no recommendation available")
    return 0


def cmd_teams(args) -> int:
    from .pipeline import Pipeline

    season = args.season or Pipeline().season()
    rows = db.query(
        "SELECT r.team, r.elo, r.power, p.exp_wins, p.playoff_prob, p.division_prob, p.sb_prob, "
        "p.wins_actual, p.losses_actual FROM team_ratings r "
        "JOIN (SELECT team, MAX(captured_at) m FROM team_ratings WHERE season=? GROUP BY team) x "
        "ON x.team = r.team AND x.m = r.captured_at "
        "LEFT JOIN season_projections p ON p.team = r.team AND p.captured_at = "
        "(SELECT MAX(captured_at) FROM season_projections WHERE team = r.team) "
        "ORDER BY r.power DESC",
        (season,),
    )
    _print_table(rows, ["team", "wins_actual", "losses_actual", "elo", "power",
                        "exp_wins", "playoff_prob", "division_prob", "sb_prob"])
    return 0


def cmd_sources(_args) -> int:
    """What data sources exist, how often they run, and what they feed."""
    from .stages import STAGES, describe

    cfg = get_config()
    rows = []
    for stage, info in zip(STAGES, describe(), strict=True):
        rows.append({
            "stage": stage.name,
            "enabled": "yes" if stage.enabled(cfg) else "no",
            "every": f"{stage.interval(cfg) / 60:.0f}m" if stage.scheduled else "on refresh",
            "feeds model": "yes" if info["feeds_model"] else "-",
            "what it is": info["description"],
        })
    _print_table(rows, ["stage", "enabled", "every", "feeds model", "what it is"])
    print("\nAdd one by writing an adapter in nflpicker/sources/, a refresh_<name>")
    print("method on the pipeline, and a Stage entry in nflpicker/stages.py.")
    return 0


def cmd_status(_args) -> int:
    from .pipeline import Pipeline

    pipeline = Pipeline()
    cfg = get_config()
    print(f"data dir     {cfg.data_dir}")
    print(f"database     {cfg.db_path} ({cfg.db_path.stat().st_size // 1024 if cfg.db_path.exists() else 0} KB)")
    print(f"mode         {'demo' if cfg.demo else 'live'}")
    print(f"odds key     {'set' if cfg.has_odds_key else 'not set'}")
    print(f"season/week  {pipeline.season()} / {pipeline.current_week()}")
    print(f"model        {pipeline.predictor.version}")
    for table in ("games", "odds_snapshots", "consensus", "predictions",
                  "season_projections", "news", "graded"):
        row = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
        print(f"  {table:<20} {row['n']:>8}")
    print("\nsources:")
    from .api import source_health

    _print_table(source_health(), ["source", "ok", "ts", "duration_ms", "detail"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nflpicker", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the dashboard and background refresher")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--log-level", default="info")
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("desktop", help="run as a native desktop window")
    p.add_argument("--width", type=int, default=1400)
    p.add_argument("--height", type=int, default=950)
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_desktop)

    p = sub.add_parser("refresh", help="fetch everything once and recompute")
    p.add_argument("--stages", help="comma separated: schedule,odds,news,stats,recompute")
    p.add_argument("--force", action="store_true", help="ignore the Odds API monthly budget")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("train", help="train the projection models")
    p.add_argument("--source", choices=["auto", "nflverse", "db"], default="auto")
    p.add_argument("--since", type=int, default=2002, help="earliest season to train on")
    p.add_argument("--no-epa", action="store_true",
                   help="skip play-by-play; trains without EPA, quarterback, "
                        "special teams or turnover features")
    p.add_argument("--fast", action="store_true", help="skip walk-forward evaluation")
    p.set_defaults(func=cmd_train)

    p = sub.add_parser("backtest", help="grade past predictions and report performance")
    p.add_argument("--season", type=int)
    p.add_argument("--regrade", action="store_true")
    p.add_argument("--backfill", action="store_true",
                   help="replay the model over games already played, then grade them")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("picks", help="print this week's recommendations")
    p.add_argument("--week", type=int)
    p.add_argument("--season", type=int)
    p.add_argument("--contest", choices=["all", "ats", "pickem", "survivor"], default="all")
    p.add_argument("--mode", choices=["ev", "leverage"], default="ev")
    p.set_defaults(func=cmd_picks)

    p = sub.add_parser("teasers", help="backtest 6-point teasers through the key numbers")
    p.add_argument("--points", type=float, default=6.0, help="teaser size in points")
    p.add_argument("--split", type=int, default=2014, help="era split season")
    p.add_argument("--sweep", action="store_true", help="also test every spread window")
    p.set_defaults(func=cmd_teasers)

    p = sub.add_parser("starters",
                       help="measure whether the announced-starter rule beats last week's")
    p.add_argument("--since", type=int, default=2021, help="earliest season")
    p.add_argument("--until", type=int, default=0, help="latest season (0 = all available)")
    p.set_defaults(func=cmd_starters)

    p = sub.add_parser("teams", help="power ratings and season projections")
    p.add_argument("--season", type=int)
    p.set_defaults(func=cmd_teams)

    p = sub.add_parser("sources", help="list data sources and their refresh intervals")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("status", help="show what is stored and which sources are healthy")
    p.set_defaults(func=cmd_status)
    return parser


# The two commands that start a server, and so leave a thread pool holding
# an in-flight fetch when they stop. Everything else is one-shot and exits the
# ordinary way. See nflpicker.desktop.leave.
SERVER_COMMANDS = {"cmd_serve", "cmd_desktop"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    get_config().ensure_dirs()
    code = args.func(args)
    if getattr(args.func, "__name__", "") in SERVER_COMMANDS:
        from .desktop import leave

        leave(code or 0)
    return code


if __name__ == "__main__":
    sys.exit(main())
