"""FastAPI application: JSON endpoints plus the single-page dashboard."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .backtest.report import performance_report
from .config import get_config
from .market import movement
from .ml.train import load_report
from .pipeline import Pipeline
from .scheduler import Scheduler
from .teams import DIVISIONS, TEAMS, reference
from .util import MARGIN_SD, margin_to_win_prob, now_iso

WEB_DIR = Path(__file__).parent / "web"


def create_app(*, start_scheduler: bool = True, bootstrap: bool = True) -> FastAPI:
    pipeline = Pipeline()
    scheduler = Scheduler(pipeline)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        if bootstrap:
            import asyncio

            # Never block startup on a slow network: the UI renders from
            # whatever is already stored and fills in when the fetch lands.
            asyncio.create_task(asyncio.to_thread(pipeline.bootstrap))
        if start_scheduler:
            await scheduler.start()
        yield
        await scheduler.stop()

    app = FastAPI(title="The Edge", version="0.1.0", lifespan=lifespan)
    app.state.pipeline = pipeline
    app.state.scheduler = scheduler

    # ------------------------------------------------------------- meta
    @app.get("/api/state")
    def state(season: int | None = None) -> dict:
        """Current state, or another season's — the week list is per season, so
        switching seasons has to reload it rather than carry the old one over."""
        asked = season
        season = season or pipeline.season()
        week = pipeline.current_week(season) if asked is None else 0
        cfg = get_config()
        last_refresh = db.get_meta("last_refresh", {}) or {}
        odds_usage: dict | None = None
        if cfg.has_odds_key and not pipeline.demo:
            with contextlib.suppress(Exception):
                from .sources.odds_api import OddsApiSource

                odds_usage = OddsApiSource().usage()
        return {
            "season": season,
            "week": week,
            "weeks": [r["week"] for r in db.query(
                "SELECT DISTINCT week FROM games WHERE season = ? AND season_type='REG' "
                "ORDER BY week", (season,))],
            "seasons": pipeline.stored_seasons(),
            "teams": reference(),
            "demo": pipeline.demo,
            "has_odds_key": cfg.has_odds_key,
            "odds_usage": odds_usage,
            "model": {
                "version": pipeline.predictor.version,
                "trained": pipeline.predictor.trained,
                "residual_sd": round(pipeline.predictor.residual_sd, 2),
                "report": load_report(),
            },
            "last_refresh": last_refresh,
            "last_recompute": db.get_meta("last_recompute"),
            "scheduler": scheduler.status(),
            "sources": source_health(),
            "server_time": now_iso(),
        }

    @app.post("/api/refresh")
    async def refresh(stages: str | None = Query(default=None)) -> dict:
        wanted = [s.strip() for s in stages.split(",")] if stages else None
        return await scheduler.refresh_now(wanted)

    # ------------------------------------------------------------ games
    @app.get("/api/games")
    def games(week: int | None = None, season: int | None = None) -> dict:
        season = season or pipeline.season()
        week = week or pipeline.current_week(season)
        return {"season": season, "week": week, "games": game_cards(season, week)}

    @app.get("/api/game/{game_id}")
    def game_detail(game_id: str) -> dict:
        game = db.query_one("SELECT * FROM games WHERE game_id = ?", (game_id,))
        if not game:
            raise HTTPException(status_code=404, detail="unknown game")
        cards = game_cards(int(game["season"]), int(game["week"]))
        card = next((c for c in cards if c["game_id"] == game_id), None)
        return {
            "game": card or game,
            "movement": {
                "spread": movement.summarise(game_id, "spread"),
                "total": movement.summarise(game_id, "total"),
            },
            "books": {
                "spread": movement.book_series(game_id, "spread"),
                "total": movement.book_series(game_id, "total"),
                "moneyline": movement.book_series(game_id, "moneyline"),
            },
            "prediction_history": db.query(
                "SELECT captured_at, margin_home, total_points, home_win_prob, "
                "market_spread, market_total, spread_edge, total_edge, model_version "
                "FROM predictions WHERE game_id = ? ORDER BY captured_at",
                (game_id,),
            ),
            "latest_books": latest_book_table(game_id),
        }

    # ------------------------------------------------------------ teams
    @app.get("/api/alerts")
    def alerts_feed(limit: int = 40, game_id: str | None = None) -> dict:
        """Alerts, optionally for one game. They are read from the game's own
        dialog now, so the common case is a single game's handful."""
        if game_id:
            rows = db.query(
                "SELECT * FROM alerts WHERE game_id = ? ORDER BY created_at DESC LIMIT ?",
                (game_id, limit))
        else:
            from . import alerts as alerts_module

            rows = alerts_module.recent(limit=limit)
        return {"alerts": rows}

    @app.post("/api/alerts/seen")
    def alerts_seen(payload: dict | None = None) -> dict:
        from . import alerts as alerts_module

        alerts_module.mark_seen((payload or {}).get("ids"))
        return {"ok": True}

    @app.get("/api/my-picks")
    def my_picks(season: int | None = None, week: int | None = None) -> dict:
        season = season or pipeline.season()
        where = "season = ?" + (" AND week = ?" if week else "")
        params = (season, week) if week else (season,)
        rows = db.query(f"SELECT * FROM user_picks WHERE {where}", params)  # noqa: S608
        return {"season": season, "week": week, "picks": rows}

    @app.post("/api/my-picks")
    def set_my_pick(payload: dict) -> dict:
        """Record or clear one pick. An empty selection removes it, so the UI
        can toggle a choice off without a second endpoint."""
        game = db.query_one("SELECT * FROM games WHERE game_id = ?",
                            (payload.get("game_id"),))
        if not game:
            raise HTTPException(status_code=404, detail="unknown game")
        contest = payload.get("contest") or "straight"
        selection = (payload.get("selection") or "").strip().upper()
        if not selection:
            db.execute(
                "DELETE FROM user_picks WHERE season = ? AND week = ? "
                "AND game_id = ? AND contest = ?",
                (game["season"], game["week"], game["game_id"], contest))
            return {"ok": True, "selection": None}
        if selection not in {game["home"], game["away"]}:
            raise HTTPException(status_code=400, detail="not a team in this game")
        db.execute(
            "INSERT OR REPLACE INTO user_picks"
            "(season, week, game_id, contest, selection, note, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (game["season"], game["week"], game["game_id"], contest, selection,
             payload.get("note"), now_iso()))
        return {"ok": True, "selection": selection}

    @app.post("/api/backfill")
    def backfill(payload: dict) -> dict:
        """Pull a season the app was not running for. Results only — a line is
        a snapshot of what was on offer at a moment, and nobody sells the past."""
        try:
            season = int(payload.get("season"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="season is required") from None
        try:
            stored = pipeline.backfill_season(season)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "season": season, "games": stored}

    @app.get("/api/scoreboard")
    def scoreboard_view(season: int | None = None) -> dict:
        from . import scoreboard

        return scoreboard.report(season or pipeline.season())

    @app.get("/api/teams")
    def teams() -> dict:
        season = pipeline.season()
        ratings = latest_team_rows(season, "team_ratings")
        projections = latest_team_rows(season, "season_projections")
        rows = []
        for abbr, team in TEAMS.items():
            rating = ratings.get(abbr, {})
            projection = projections.get(abbr, {})
            distribution = {}
            with contextlib.suppress(Exception):
                distribution = json.loads(projection.get("distribution") or "{}")
            rows.append(
                {
                    "team": abbr,
                    "name": team.full_name,
                    "division": team.division,
                    "conference": team.conference,
                    "color": team.color,
                    "elo": rating.get("elo"),
                    "power": rating.get("power"),
                    "off_rating": rating.get("off_rating"),
                    "def_rating": rating.get("def_rating"),
                    "off_epa": rating.get("off_epa"),
                    "def_epa": rating.get("def_epa"),
                    "record": {
                        "wins": projection.get("wins_actual"),
                        "losses": projection.get("losses_actual"),
                        "ties": projection.get("ties_actual"),
                    },
                    "exp_wins": projection.get("exp_wins"),
                    "pythagorean": rating.get("pythagorean"),
                    "wins_p10": projection.get("wins_p10"),
                    "wins_p90": projection.get("wins_p90"),
                    "playoff_prob": projection.get("playoff_prob"),
                    "division_prob": projection.get("division_prob"),
                    "bye_prob": projection.get("bye_prob"),
                    "sb_prob": projection.get("sb_prob"),
                    "win_total_line": projection.get("win_total_line"),
                    "over_prob": projection.get("over_prob"),
                    "distribution": distribution,
                }
            )
        # Ranked by where each team is projected to *finish*, not by how it has
        # gone so far. That is what a power ranking is for: expected wins comes
        # out of 20,000 simulations of the remaining schedule, so it already
        # carries both the rating and who is left to play. The rating breaks
        # ties, because two teams can project to the same win total off very
        # different strength.
        order = {team: i for i, team in enumerate(
            team_rank_order(season, ratings, projections), start=1)}
        rows.sort(key=lambda r: order.get(r["team"], 99))
        for i, row in enumerate(rows, start=1):
            row["rank"] = i
        # Where the projection disagrees with the table is the interesting
        # column: a team five places better than its record is one the model
        # thinks has been unlucky.
        by_record = sorted(
            rows,
            key=lambda r: -((r["record"] or {}).get("wins") or 0)
            + ((r["record"] or {}).get("losses") or 0) * 0.001)
        record_rank = {r["team"]: i for i, r in enumerate(by_record, start=1)}
        for row in rows:
            row["record_rank"] = record_rank.get(row["team"])
        return {"season": season, "teams": rows, "divisions": DIVISIONS}

    @app.get("/api/team/{abbr}/history")
    def team_history(abbr: str) -> dict:
        abbr = abbr.upper()
        return {
            "team": abbr,
            "ratings": db.query(
                "SELECT captured_at, elo, power, off_rating, def_rating "
                "FROM team_ratings WHERE team = ? ORDER BY captured_at", (abbr,)
            ),
            "projections": db.query(
                "SELECT captured_at, exp_wins, playoff_prob, division_prob, sb_prob "
                "FROM season_projections WHERE team = ? ORDER BY captured_at", (abbr,)
            ),
        }

    # ------------------------------------------------------------ picks
    @app.get("/api/picks")
    def picks(week: int | None = None, season: int | None = None) -> dict:
        season = season or pipeline.season()
        week = week or pipeline.current_week(season)
        return {
            "season": season,
            "week": week,
            "ats": latest_pick("ats", season, week),
            "prediction_markets": latest_pick("prediction_markets", season, week),
            "pickem": latest_pick("pickem", season, week),
            "survivor": latest_pick("survivor", season, week),
            "survivor_used": db.get_meta("survivor_used_teams", []) or [],
        }

    @app.post("/api/survivor/used")
    def set_survivor_used(payload: dict[str, Any]) -> dict:
        teams_used = [str(t).upper() for t in (payload.get("teams") or []) if t]
        unknown = [t for t in teams_used if t not in TEAMS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown teams: {unknown}")
        db.set_meta("survivor_used_teams", teams_used)
        pipeline.recompute()
        return {"teams": teams_used, "ok": True}

    @app.get("/api/picks/history")
    def pick_history(contest: str = "ats", season: int | None = None) -> dict:
        season = season or pipeline.season()
        rows = db.query(
            "SELECT week, captured_at, payload FROM pick_history "
            "WHERE contest = ? AND season = ? ORDER BY week DESC, captured_at DESC",
            (contest, season),
        )
        for row in rows:
            with contextlib.suppress(Exception):
                row["payload"] = json.loads(row["payload"])
        return {"contest": contest, "season": season, "entries": rows}

    # ------------------------------------------------------------- news

    # --------------------------------------------------------- settings
    @app.get("/api/settings")
    def read_settings() -> dict:
        from . import settings as settings_module

        return settings_module.current()

    @app.post("/api/settings")
    def write_settings(payload: dict) -> dict:
        """Save settings. Only keys this app defines are written, and a key the
        UI did not send is left alone rather than cleared -- otherwise opening
        the page and saving one field would wipe a secret it never displayed."""
        from . import settings as settings_module

        values = payload.get("values")
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="expected a values object")
        # save() resets the cached config, and the scheduler calls get_config()
        # afresh on every cycle, so a changed cadence or a new key is picked up
        # on the next pass without a restart.
        return settings_module.save(values)

    @app.get("/api/settings/backups")
    def list_backups() -> dict:
        from . import settings as settings_module

        return {"backups": settings_module.list_backups(),
                "directory": str(settings_module.backup_dir()),
                "keep": settings_module.BACKUP_KEEP}

    @app.post("/api/settings/backup")
    def make_backup() -> dict:
        from . import settings as settings_module

        try:
            return settings_module.make_backup()
        except settings_module.SettingsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Could not write the backup: {exc}") from exc

    @app.post("/api/settings/test-odds-key")
    def test_odds_key(payload: dict) -> dict:
        from . import settings as settings_module

        return settings_module.validate_odds_key(payload.get("key") or "")

    # -------------------------------------------------------- assistant
    @app.get("/api/assistant/status")
    def assistant_status() -> dict:
        from . import assistant

        return assistant.status()

    @app.post("/api/assistant/ask")
    def assistant_ask(payload: dict) -> dict:
        from . import assistant

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="expected messages")
        season = int(payload.get("season") or pipeline.season())
        week = int(payload.get("week") or pipeline.current_week(season))
        try:
            return assistant.ask(messages, season, week)
        except assistant.AssistantError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/news")
    def news(limit: int = 60, min_impact: float = 0.0, team: str | None = None) -> dict:
        sql = "SELECT * FROM news WHERE impact >= ?"
        params: list[Any] = [min_impact]
        if team:
            sql += " AND teams LIKE ?"
            params.append(f'%"{team.upper()}"%')
        sql += " ORDER BY impact DESC, published_at DESC LIMIT ?"
        params.append(limit)
        rows = db.query(sql, params)
        for row in rows:
            with contextlib.suppress(Exception):
                row["teams"] = json.loads(row["teams"] or "[]")
        # Only players whose availability is in question. An injury table is
        # mostly the word "Active" repeated, and a row that says a healthy
        # player is healthy is not an injury report -- it buries the four names
        # that actually change a projection.
        injuries = [
            r for r in db.query(
                "SELECT team, player, position, status, detail, MAX(updated_at) AS updated_at "
                "FROM injuries GROUP BY team, player ORDER BY team, player"
            )
            if _is_notable_injury(r.get("status"))
        ]
        return {
            "items": rows,
            "injuries": [r for r in injuries
                         if not team or r["team"] == team.upper()],
            # Every team that has someone listed, so the picker can grey out
            # the ones with a clean sheet rather than offering an empty page.
            "injury_teams": sorted({r["team"] for r in injuries}),
            "injury_counts": {
                t: sum(1 for r in injuries if r["team"] == t)
                for t in sorted({r["team"] for r in injuries})
            },
        }

    # ------------------------------------------------------- performance
    @app.get("/api/edge")
    def edge(season: int | None = None) -> dict:
        """Whether the line moves toward us — the sharpest test available."""
        from .market.opening import coverage, movement_report

        return {
            "movement": movement_report(season if season else None),
            "coverage": coverage(),
            "clv": performance_report(season if season else None).get("clv"),
        }

    @app.get("/api/performance")
    def performance(season: int | None = None) -> dict:
        report = performance_report(season if season else None)
        # The walk-forward numbers are the honest benchmark; ship them together
        # so the UI can show them beside any in-sample backfill figures.
        training = load_report() or {}
        report["walk_forward"] = training.get("blind")
        report["walk_forward_market"] = {
            "margin_mae": (training.get("blind") or {}).get("market_margin_mae"),
        }
        return report

    # -------------------------------------------------------------- web
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    @app.exception_handler(Exception)
    async def unhandled(_request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return app


# --------------------------------------------------------------- helpers

# Feeds the app is built to run without. Their absence changes what is on
# screen -- a column reads "-" -- but nothing downstream is wrong, so the
# status strip marks them amber rather than red. A red dot should mean
# something is broken, and if everything optional reports red on a normal day
# the strip stops being read at all.
OPTIONAL_SOURCES = {"prediction_markets", "news", "weather"}


def source_health() -> list[dict]:
    """Latest outcome per source, for the status strip in the UI."""
    rows = db.query(
        "SELECT f.source, f.ok, f.ts, f.detail, f.duration_ms FROM fetch_log f "
        "JOIN (SELECT source, MAX(id) AS m FROM fetch_log GROUP BY source) x "
        "ON x.m = f.id ORDER BY f.source"
    )
    for row in rows:
        row["optional"] = row["source"] in OPTIONAL_SOURCES
    return rows


def latest_pick(contest: str, season: int, week: int) -> Any:
    row = db.query_one(
        "SELECT payload FROM pick_history WHERE contest = ? AND season = ? AND week = ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (contest, season, week),
    )
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def latest_book_table(game_id: str) -> list[dict]:
    """Each book's current spread, total and moneyline, side by side."""
    rows = db.query(
        "SELECT o.book, o.market, o.home_point, o.away_point, o.home_price, o.away_price, "
        "o.captured_at FROM odds_snapshots o JOIN (SELECT book, market, MAX(captured_at) AS m "
        "FROM odds_snapshots WHERE game_id = ? GROUP BY book, market) x "
        "ON x.book = o.book AND x.market = o.market AND x.m = o.captured_at "
        "WHERE o.game_id = ?",
        (game_id, game_id),
    )
    by_book: dict[str, dict] = {}
    for row in rows:
        entry = by_book.setdefault(row["book"], {"book": row["book"], "captured_at": row["captured_at"]})
        if row["market"] == "spread":
            entry["spread"] = row["home_point"]
            entry["spread_price_home"] = row["home_price"]
            entry["spread_price_away"] = row["away_price"]
        elif row["market"] == "total":
            entry["total"] = row["home_point"]
            entry["over_price"] = row["home_price"]
            entry["under_price"] = row["away_price"]
        elif row["market"] == "moneyline":
            entry["ml_home"] = row["home_price"]
            entry["ml_away"] = row["away_price"]
    return sorted(by_book.values(), key=lambda b: b["book"])


def _opener_edge(prediction: dict | None, move: dict) -> float | None:
    """How far our number sits from the line *as it opened*.

    Deliberately not a training target and not the number the app recommends
    on. The closing line is the sharp one and the model does not beat it; the
    opener is the same market before it has been corrected, which is the only
    place a disagreement is worth a second look. Reporting it separately keeps
    that distinction visible instead of quietly blending two different claims.

    Positive means we like the home side more than the opening line did.
    """
    if not prediction:
        return None
    opening = move.get("open")
    fair = prediction.get("fair_margin")
    if opening is None or fair is None:
        return None
    return round(float(fair) - (-float(opening)), 2) or 0.0


def _moved_toward_us(prediction: dict | None, move: dict) -> float | None:
    """Points the line has moved toward the side we favour, since it opened.

    Positive means the market has come our way, which is the only evidence
    available before a game finishes that a disagreement was worth having.
    Negative means it moved against us; near zero means it has not moved.

    The sign convention is the one in :mod:`nflpicker.market.opening`, kept
    identical on purpose: a market's implied home margin is the negation of the
    posted home line, and our lean is how much more we like the home side than
    that. Deriving it in the client too would be a second place to get a
    negation wrong.
    """
    if not prediction:
        return None
    opening, current = move.get("open"), move.get("current")
    fair = prediction.get("fair_margin")
    if opening is None or current is None or fair is None:
        return None
    lean = float(fair) - (-float(opening))
    movement = (-float(current)) - (-float(opening))
    if abs(lean) < 1e-9:
        return 0.0
    # `or 0.0` collapses -0.0, which would otherwise render as "-0.0".
    return round(movement if lean > 0 else -movement, 2) or 0.0


# Statuses that mean "this player may not play". Anything else -- Active,
# a blank, a status a feed invented -- is not a report, and showing it turns a
# four-name list into a four-hundred-name one.
_NOTABLE_INJURY = {
    "out", "doubtful", "questionable", "injured reserve", "ir",
    "physically unable to perform", "pup", "did not participate",
    "limited participation", "non football injury", "nfi", "suspended",
    "reserve/covid-19", "practice squad/injured",
}


def _is_notable_injury(status: str | None) -> bool:
    value = (status or "").strip().lower()
    if not value or value in {"active", "full participation", "probable", "healthy"}:
        return False
    return value in _NOTABLE_INJURY or any(k in value for k in ("out", "doubtful",
                                                                "questionable",
                                                                "reserve", "pup",
                                                                "injured"))


def latest_team_rows(season: int, table: str, columns: str = "*") -> dict[str, dict]:
    """The most recent row per team from a captured_at-stamped table.

    Four call sites had this same correlated-subquery join written out longhand,
    which is how the Teams endpoint ended up fetching ratings and projections
    twice per request — once for the payload and once to sort it.
    """
    if table not in {"team_ratings", "season_projections"}:
        raise ValueError(f"not a team table: {table}")
    return {
        r["team"]: r
        for r in db.query(
            f"SELECT {columns} FROM {table} t JOIN (SELECT team, MAX(captured_at) m "  # noqa: S608
            f"FROM {table} WHERE season = ? GROUP BY team) x "
            f"ON x.team = t.team AND x.m = t.captured_at", (season,))
    }


def team_rank_order(season: int, ratings: dict | None = None,
                    projections: dict | None = None) -> list[str]:
    """Our teams, best first — the one ordering the whole app calls "our rank".

    It exists because there were briefly two. The Teams page ranks by projected
    finish while the ranking comparison ranked by rating, so the "ours" column
    beside the consensus disagreed with the rank printed two panels away, and
    nothing on either screen said why. Callers that have already fetched the two
    tables pass them in rather than paying for them again.
    """
    if ratings is None:
        ratings = latest_team_rows(season, "team_ratings")
    if projections is None:
        projections = latest_team_rows(season, "season_projections")
    ratings = {t: r["power"] if isinstance(r, dict) else r for t, r in ratings.items()}
    projections = {t: p["exp_wins"] if isinstance(p, dict) else p
                   for t, p in projections.items()}
    teams = sorted(set(ratings) | set(projections))
    # Projected wins first, rating as the tie-break: two teams can project to
    # the same total off very different strength.
    teams.sort(key=lambda t: (
        projections.get(t) is None and ratings.get(t) is None,
        -(projections[t] if projections.get(t) is not None else -99),
        -(ratings.get(t) or 0),
    ))
    return teams


def game_cards(season: int, week: int) -> list[dict]:
    """Everything the UI needs to render one week's games."""
    games = db.query(
        "SELECT * FROM games WHERE season = ? AND week = ? ORDER BY kickoff, game_id",
        (season, week),
    )
    if not games:
        return []
    ids = [g["game_id"] for g in games]
    placeholders = ",".join("?" for _ in ids)

    predictions = {
        r["game_id"]: r
        for r in db.query(
            f"SELECT p.* FROM predictions p JOIN (SELECT game_id, MAX(captured_at) m "
            f"FROM predictions WHERE game_id IN ({placeholders}) GROUP BY game_id) x "
            "ON x.game_id = p.game_id AND x.m = p.captured_at",
            ids,
        )
    }
    consensus = {
        r["game_id"]: r
        for r in db.query(
            f"SELECT c.* FROM consensus c JOIN (SELECT game_id, MAX(captured_at) m "
            f"FROM consensus WHERE game_id IN ({placeholders}) GROUP BY game_id) x "
            "ON x.game_id = c.game_id AND x.m = c.captured_at",
            ids,
        )
    }
    # The board shows the blind model as its own column, which needs a
    # probability and not just a margin. Only the margin is stored -- the
    # probability the row carries is built from the *blend* -- so it is derived
    # here, once, from the same residual spread the predictor uses. Doing it in
    # the browser would put a second copy of this arithmetic somewhere it could
    # drift from the first.
    blind_sd = float((load_report() or {}).get("residual_sd") or MARGIN_SD)
    for row in predictions.values():
        margin = row.get("margin_home")
        row["blind_win_prob"] = (
            None if margin is None else float(margin_to_win_prob(float(margin), sd=blind_sd)))

    graded = {r["game_id"]: r for r in db.query(
        f"SELECT * FROM graded WHERE game_id IN ({placeholders})", ids)}
    live = {r["game_id"]: r for r in db.query(
        f"SELECT * FROM live_state WHERE game_id IN ({placeholders})", ids)}
    weather = {r["game_id"]: r for r in db.query(
        f"SELECT * FROM game_weather WHERE game_id IN ({placeholders})", ids)}

    news_items = db.query(
        "SELECT id, title, url, source, published_at, teams, category, impact, line_impact "
        "FROM news WHERE impact >= 0.3 ORDER BY impact DESC, published_at DESC LIMIT 120"
    )
    for item in news_items:
        with contextlib.suppress(Exception):
            item["teams"] = json.loads(item["teams"] or "[]")
    from .news.impact import affected_games

    news_by_game = affected_games(news_items, games)
    availability = db.get_meta("availability", {}) or {}

    cards = []
    for game in games:
        gid = game["game_id"]
        prediction = predictions.get(gid)
        market = consensus.get(gid)
        components = {}
        if prediction:
            with contextlib.suppress(Exception):
                components = json.loads(prediction.get("components") or "{}")
        move = movement.summarise(gid, "spread")
        total_move = movement.summarise(gid, "total")
        cards.append(
            {
                "game_id": gid,
                "season": game["season"],
                "week": game["week"],
                "kickoff": game["kickoff"],
                "status": game["status"],
                "home": game["home"],
                "away": game["away"],
                "home_name": TEAMS[game["home"]].full_name if game["home"] in TEAMS else game["home"],
                "away_name": TEAMS[game["away"]].full_name if game["away"] in TEAMS else game["away"],
                "home_color": TEAMS[game["home"]].color if game["home"] in TEAMS else "#888",
                "away_color": TEAMS[game["away"]].color if game["away"] in TEAMS else "#888",
                "home_score": game["home_score"],
                "away_score": game["away_score"],
                "venue": game["venue"],
                "prediction": prediction,
                "components": components,
                "market": market,
                "movement": {
                    "spread_open": move["open"], "spread_now": move["current"],
                    "spread_move": move["move"], "steam": move["steam"],
                    "total_open": total_move["open"], "total_now": total_move["current"],
                    "total_move": total_move["move"],
                    "toward_us": _moved_toward_us(prediction, move),
                    "opener_edge": _opener_edge(prediction, move),
                    "points": move["points"][-40:],
                },
                "graded": graded.get(gid),
                "live": live.get(gid),
                "weather": weather.get(gid),
                "news": news_by_game.get(gid, []),
                "availability": {
                    "home": availability.get(game["home"]),
                    "away": availability.get(game["away"]),
                },
            }
        )
    return cards
