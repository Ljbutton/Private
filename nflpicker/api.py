"""FastAPI application: JSON endpoints plus the single-page dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, buildinfo, db, identity, licensing
from .availability import is_notable_injury
from .config import get_config
from .market import movement
from .ml.train import load_report
from .pipeline import Pipeline
from .scheduler import Scheduler
from .teams import DIVISIONS, TEAMS, reference
from .util import MARGIN_SD, margin_to_win_prob, now_iso

WEB_DIR = Path(__file__).parent / "web"


def _sse(data: dict) -> str:
    """One server-sent event carrying one JSON object.

    The blank line is the record separator the format requires; without it
    the browser holds everything until the connection closes, which is
    exactly the behaviour this endpoint exists to avoid.
    """
    return f"data: {json.dumps(data)}\n\n"


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
        # The caller may say no; so may the setting. Automatic polling is off
        # unless someone asked for it -- the refresh button does everything the
        # timers did, when you want it done rather than every few minutes.
        if start_scheduler and get_config().auto_refresh:
            await scheduler.start()
        yield
        await scheduler.stop()
        # A model server this app started is this app's to shut down. One left
        # running after the window closes is a gigabyte of somebody's memory
        # held by a program they think they quit.
        with contextlib.suppress(Exception):
            from . import localmodel

            localmodel.stop()

    app = FastAPI(title="The Edge", version="0.1.0", lifespan=lifespan)
    app.state.pipeline = pipeline
    app.state.scheduler = scheduler

    # ---------------------------------------------------------- license
    # Everything under /api needs a licensed copy, except the calls that get
    # it licensed and the update check. The page itself and its scripts are
    # always served, so the activation screen can load.
    #
    # Support is open for the same reason activation is. "My key will not
    # activate" is the likeliest report this app will ever receive, and the
    # people making it are by definition the ones stuck behind this gate --
    # a reporting flow they cannot reach is a reporting flow for everybody
    # except the users who most need it.
    OPEN_PATHS = ("/api/license", "/api/update", "/api/support")

    @app.middleware("http")
    async def require_license(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not path.startswith(OPEN_PATHS):
            if not licensing.allowed():
                return JSONResponse(status_code=402, content={
                    "error": "license_required", "license": licensing.status()})
            licensing.recheck_in_background()
        return await call_next(request)

    @app.get("/api/license")
    def license_status() -> dict:
        return licensing.status()

    @app.post("/api/license/activate")
    def license_activate(payload: dict = Body(default={})) -> dict:  # noqa: B008
        return licensing.activate(str(payload.get("key") or ""))

    @app.post("/api/license/recheck")
    def license_recheck() -> dict:
        return licensing.recheck(force=True)

    @app.post("/api/license/deactivate")
    def license_deactivate() -> dict:
        return licensing.deactivate()

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
            # The same list the selector draws, with the postseason on the end
            # under its own names. `weeks` stays the regular season alone,
            # because that is what survivor and the pick board are about --
            # there is no survivor pool in January and no pick'em board for a
            # bracket. Anything that offers a week to choose reads this;
            # anything that reasons about the season reads `weeks`.
            "week_options": week_options(season),
            "seasons": pipeline.stored_seasons(),
            "teams": reference(),
            "demo": pipeline.demo,
            # Read from this computer's account so the app can say hello. It
            # is served to the page and goes nowhere else.
            "user": identity.greeting_name(),
            "has_odds_key": cfg.has_odds_key,
            # The handful of settings the page's chrome reads, rather than the
            # whole of Settings: this is fetched every minute and most of what
            # is in there is a secret or a cadence the browser has no use for.
            "settings": {
                "assistant_button": _setting_is_on("NFLPICKER_ASSISTANT_BUTTON"),
            },
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
            # Which build this is. Without it, "still broken" and "already
            # fixed" are both unfalsifiable and a report can chase a fix round
            # in circles -- the window looks identical either way.
            "build": buildinfo.build_info(),
            "build_label": buildinfo.label(),
            # The version a person can say out loud. The build commit answers
            # "is this the copy with the fix"; this answers "which version am
            # I on", and a seven-character hash has never answered that.
            "version": __version__,
        }

    @app.get("/api/live")
    async def live(season: int | None = None, week: int | None = None) -> dict:
        """The scoreboard, on its own clock.

        Scores and the game clock are the only thing in this app that is
        stale within seconds, and they were riding on the same tick as the
        model: the page asked for a full refresh once a minute, the schedule
        stage declined because its five-minute interval had not elapsed, and
        the board sat on a score from four minutes ago. Everything else here
        -- ratings, projections, the season simulation -- changes when a game
        *ends*, not while it is being played.

        So this is its own endpoint: one week of ESPN's scoreboard, an upsert,
        and the live win probability. No stages, no recompute, no lock shared
        with the refresh, and cheap enough for the page to call every few
        seconds. The fetch itself is rate-limited in the pipeline, so several
        open tabs cost one request between them.
        """
        season = season or pipeline.season()
        week = week or pipeline.current_week(season)
        result = await asyncio.to_thread(pipeline.refresh_live, season, week)
        rows = db.query(
            "SELECT g.game_id, g.week, g.kickoff, g.status, g.home, g.away,"
            " g.home_score, g.away_score,"
            " l.period, l.clock, l.down, l.distance, l.red_zone,"
            " l.win_prob_home AS live_home_win_prob "
            "FROM games g LEFT JOIN live_state l ON l.game_id = g.game_id "
            "WHERE g.season = ? AND g.week = ? AND g.season_type = 'REG'",
            (season, week),
        )
        games = []
        for r in rows:
            live = None
            if r["status"] == "in_progress" and r["period"] is not None:
                live = {"period": r["period"], "clock": r["clock"],
                        "down": r["down"], "distance": r["distance"],
                        "red_zone": bool(r["red_zone"]),
                        "home_win_prob": r["live_home_win_prob"]}
            games.append({
                "game_id": r["game_id"], "week": r["week"], "status": r["status"],
                "kickoff": r["kickoff"],
                "home": r["home"], "away": r["away"],
                "home_score": r["home_score"], "away_score": r["away_score"],
                "live": live,
            })
        return {"season": season, "week": week, "games": games, **result}

    @app.post("/api/refresh")
    async def refresh(stages: str | None = Query(default=None),
                      full: bool = Query(default=False)) -> dict:
        wanted = [s.strip() for s in stages.split(",")] if stages else None
        return await scheduler.refresh_now(wanted, full=full)

    @app.get("/api/support/details")
    def support_details() -> dict:
        """What the form would attach, so it can be read before it is sent.

        The page shows this in full behind a disclosure rather than promising
        it: "we'll include your log" is worth nothing next to the log itself,
        and the one thing a person wants to check before mailing a stranger
        their log is what is in it.
        """
        from . import support

        return {
            "details": support.details(None),
            "keys": list(support.DETAIL_KEYS),
            "categories": [{"key": key, "label": label}
                           for key, label in support.CATEGORIES.items()],
            "images": {"max": support.MAX_IMAGES,
                       "max_bytes": support.MAX_IMAGE_BYTES},
            "store_url": licensing.store_url(),
            "can_send": bool(licensing.server_url()),
        }

    @app.post("/api/support/report")
    def support_report(payload: dict = Body(default={})) -> dict:  # noqa: B008
        """Take a message for support, redact it, and forward it for emailing.

        Open before activation -- see OPEN_PATHS. The description is the only
        required field; everything else is either optional or a box the user
        can untick, and what is unticked is never gathered. Images are the one
        thing that cannot be redacted, so they are only ever the ones the
        reporter attached by hand.
        """
        from . import support

        description = str(payload.get("description") or "").strip()
        if not description:
            raise HTTPException(status_code=400,
                                detail="A description is required.")
        report = support.build(
            description,
            category=payload.get("category"),
            doing=str(payload.get("doing") or ""),
            email=str(payload.get("email") or ""),
            include=payload.get("include") if isinstance(
                payload.get("include"), dict) else None,
            images=payload.get("images") if isinstance(
                payload.get("images"), list) else None,
        )
        return support.forward(report)

    @app.get("/api/updates")
    def updates_check(force: bool = Query(default=False)) -> dict:
        """Whether a newer build has been published.

        Its own endpoint rather than a field on /api/state, because that is
        fetched every minute and this asks the internet. Cached for a day.
        """
        from . import updates

        return updates.check(force=force)

    @app.post("/api/refresh/odds")
    async def refresh_odds() -> dict:
        """The betting lines, on their own, because they are the metered feed.

        Three requests out of a monthly allowance, every time. That is a
        deliberate spend and it gets a deliberate button; nothing else in the
        app is allowed to reach it, so a refresh of the scores can never
        quietly cost a credit.
        """
        result = await scheduler.refresh_now(["odds"])
        usage = None
        with contextlib.suppress(Exception):
            from .sources.odds_api import OddsApiSource

            usage = OddsApiSource().usage()
        return {**result, "usage": usage}

    # ------------------------------------------------------------ games
    def straight_record(season: int, week: int | None = None) -> dict:
        """Your own straight-up record: won, lost, and how many are still out.

        One query over two indexed columns, rather than the grading pass
        Performance runs. That pass scores four pickers against each other and
        is the right tool for the question "is the blend adding anything"; this
        answers "how am I doing", which is a count.

        A pick on a game that has not finished is neither a win nor a loss and
        is reported separately -- folding it into either would mean the record
        moved every time a game kicked off.
        """
        where = "p.season = ?" + (" AND p.week = ?" if week else "")
        params = (season, week) if week else (season,)
        row = db.query_one(
            "SELECT "
            " SUM(g.status = 'final' AND ("
            "   (g.home_score > g.away_score AND p.selection = g.home) OR"
            "   (g.away_score > g.home_score AND p.selection = g.away))) won,"
            " SUM(g.status = 'final' AND ("
            "   (g.home_score > g.away_score AND p.selection = g.away) OR"
            "   (g.away_score > g.home_score AND p.selection = g.home))) lost,"
            " SUM(g.status = 'final' AND g.home_score = g.away_score) tied,"
            " SUM(g.status != 'final') pending "
            "FROM user_picks p JOIN games g ON g.game_id = p.game_id "
            f"WHERE {where} AND p.contest = 'straight'",  # noqa: S608
            params,
        ) or {}
        return {key: int(row.get(key) or 0)
                for key in ("won", "lost", "tied", "pending")}

    @app.get("/api/games")
    def games(week: int | None = None, season: int | None = None) -> dict:
        season = season or pipeline.season()
        week = week or pipeline.current_week(season)
        # A playoff round is a week like any other as far as the board is
        # concerned; it just lives under a different season_type with its own
        # numbering. Byes and survivor do not apply to it -- fourteen teams
        # are not on a bye in January, they are out.
        if week_kind(season, week) == "POST":
            return {
                "season": season, "week": week, "season_type": "POST",
                "games": game_cards(season, week, season_type="POST"),
                "byes": [], "survivor_pick": None, "survivor_used_weeks": {},
            }

        # Who is not playing, and what there is to say about them. A bye week
        # leaves a hole in the board where two or three games would be; the
        # teams that made the hole are the obvious thing to put in it.
        ranks = {r["team"]: r for r in db.query(
            "SELECT team, rank, power FROM power_snapshots "
            "WHERE season = ? AND week = ? AND source = 'live'", (season, week))}
        records = records_before(season, week)
        on_bye = teams_on_bye(season, week)
        # A bye team's week is its season: there is no game to open, so what
        # the card has to answer is where the year is going. The projections
        # are already loaded per team, and who they play next comes off the
        # schedule the board is reading anyway -- fetching both here means a
        # click on a bye opens something rather than a second round trip.
        projections = (latest_team_rows(season, "season_projections")
                       if on_bye else {})
        next_games = _next_opponents(season, week, on_bye) if on_bye else {}
        byes = [
            {
                "team": team,
                "name": TEAMS[team].name if team in TEAMS else team,
                "full_name": TEAMS[team].full_name if team in TEAMS else team,
                "conference": TEAMS[team].conference if team in TEAMS else "",
                "division": TEAMS[team].division if team in TEAMS else "",
                "record": records.get(team, ""),
                "rank": (ranks.get(team) or {}).get("rank"),
                "power": (ranks.get(team) or {}).get("power"),
                "next": next_games.get(team),
                **{
                    key: (projections.get(team) or {}).get(key)
                    for key in ("playoff_prob", "division_prob", "bye_prob",
                                "sb_prob", "exp_wins", "wins_p10", "wins_p90")
                },
            }
            for team in on_bye
        ]
        return {
            "season": season, "week": week,
            "games": game_cards(season, week),
            "byes": byes,
            "survivor_pick": survivor_pick_for(season, week),
            # Every other week a team has been spent in, so the board can show
            # one as unavailable instead of quietly moving the pick.
            "survivor_used_weeks": {
                team: wk for wk, team in survivor_picks(season).items()
                if int(wk) != int(week)
            },
            # How your own picks are going, for the week on screen and for the
            # season. It rides along with the board because the board is what
            # the question is asked in front of.
            "my_record": {
                "week": straight_record(season, week),
                "season": straight_record(season),
            },
        }

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
    def teams(season: int | None = None, week: int | None = None) -> dict:
        """The ranking as it stood in a given week.

        It used to take no arguments at all. The week selector is on every
        page and the page it drives hardest is this one, but the query string
        it sent was discarded here and the answer was always the current
        week's cut -- so a season's worth of frozen rankings existed in the
        database and there was no way to look at any of them. Choosing week
        two in December showed December's table with week two in the header,
        which is the one thing a frozen ranking is supposed to make
        impossible.
        """
        season = season or pipeline.season()
        week = week or pipeline.current_week(season)
        ratings = latest_team_rows(season, "team_ratings")
        projections = latest_team_rows(season, "season_projections")

        # That week's cut, if it has been taken: its order, its records and the
        # projection it was ordered by. All three or none of them -- freezing
        # the rank and leaving the rest of the row live is what put a 2-0
        # record in week two's table, beside a ranking that was taken before
        # those games were played.
        cut_rows = db.query(
            "SELECT team, rank, power, pythagorean, wins, losses, ties, projection "
            "FROM power_snapshots WHERE season = ? AND week = ? AND source = 'live'",
            (season, week),
        )
        cut = {r["team"]: r for r in cut_rows}
        if cut:
            for abbr, row in cut.items():
                frozen = {}
                with contextlib.suppress(Exception):
                    frozen = json.loads(row["projection"] or "{}") or {}
                frozen.setdefault("wins_actual", row["wins"])
                frozen.setdefault("losses_actual", row["losses"])
                frozen.setdefault("ties_actual", row["ties"])
                # The distribution is not stored with the cut, so it is carried
                # over from the live projection: it is a shape, and it is the
                # one thing on the page that is drawn rather than claimed.
                live = projections.get(abbr) or {}
                if live.get("distribution") is not None:
                    frozen.setdefault("distribution", live["distribution"])
                projections[abbr] = {**live, **frozen}
                rating = dict(ratings.get(abbr) or {})
                rating["power"] = row["power"]
                rating["pythagorean"] = row["pythagorean"]
                ratings[abbr] = rating
        # Points for and against, from the games themselves.
        #
        # The Pythagorean is stored on the rating row, and on a database that
        # predates the column -- or any row written before a recompute filled
        # it in -- that field is null and the card showed a dash where the
        # number belongs. It is not worth a dash: it is two sums over games we
        # already hold, so it is computed here when the stored one is missing
        # rather than waiting for the next full recompute to backfill it.
        scored: dict[str, list[float]] = {}
        for game in db.query(
            "SELECT home, away, home_score, away_score FROM games "
            "WHERE season = ? AND status = 'final' "
            "AND home_score IS NOT NULL AND away_score IS NOT NULL",
            (season,),
        ):
            for team, pf, pa in ((game["home"], game["home_score"], game["away_score"]),
                                 (game["away"], game["away_score"], game["home_score"])):
                got = scored.setdefault(team, [0.0, 0.0])
                got[0] += float(pf)
                got[1] += float(pa)
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
                    "pythagorean": _pythagorean(rating, scored.get(abbr)),
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
        # Ranked by how good a team is rather than by how its Sundays have
        # gone -- see RANK_WEIGHTS. The score is carried on the row so the
        # card can show what put a team where it is.
        scores = ranking_scores(ratings, projections)
        # This week's cut if it has been taken, and the live blend until then.
        #
        # The ranking is a claim made on a particular day -- Wednesday, by
        # default -- from what was known then, and it holds for the week. It
        # used to be recomputed on every refresh, which meant the table quietly
        # reordered itself several times a day and the Move column compared two
        # numbers that had both moved since anyone last looked. What the cut
        # does not freeze is the rest of the row: the projections, the playoff
        # odds and the records are forecasts and results, and a forecast that
        # ignored Sunday would just be wrong.
        order = ({t: r["rank"] for t, r in cut.items()} if cut else
                 {team: i for i, team in enumerate(
                     team_rank_order(season, ratings, projections), start=1)})
        # A team the cut does not name -- it cannot happen mid-season, but a
        # partial write would put someone at the top by accident -- goes last
        # in the live order rather than first.
        fallback = {team: i for i, team in enumerate(
            team_rank_order(season, ratings, projections), start=1)}
        rows.sort(key=lambda r: order.get(r["team"],
                                          99 + fallback.get(r["team"], 99)))
        for row in rows:
            row["rank_score"] = round(scores.get(row["team"], 0.0), 3)
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

    @app.get("/api/team/{abbr}")
    def team_detail(abbr: str, season: int | None = None,
                    week: int | None = None) -> dict:
        """One team's season, for the card a click on its name opens.

        The bye cards grew this first, because a team with no game this week
        has nothing else to show. It turned out to be the more useful answer
        for every other team too -- a game card says what happens on Sunday,
        and "are they actually any good, and where does this end up" is the
        question the crest invites. So it is an endpoint rather than something
        the bye payload carries, and both callers read the same one.
        """
        abbr = abbr.strip().upper()
        if abbr not in TEAMS:
            raise HTTPException(status_code=404, detail=f"unknown team: {abbr}")
        season = season or pipeline.season()
        week = week or pipeline.current_week(season)
        team = TEAMS[abbr]

        projection = (latest_team_rows(season, "season_projections")
                      .get(abbr) or {})
        rating = latest_team_rows(season, "team_ratings").get(abbr) or {}
        cut = db.query_one(
            "SELECT rank, power FROM power_snapshots WHERE season = ? "
            "AND week = ? AND source = 'live' AND team = ?", (season, week, abbr),
        ) or {}

        # This week's game and the next one are different questions and a team
        # can have either, both or neither: on a bye there is no game now but
        # there is one coming, and in week 18 it is the other way round.
        this_week = db.query_one(
            "SELECT game_id, week, home, away, kickoff, status, home_score,"
            " away_score FROM games WHERE season = ? AND week = ? "
            "AND season_type = 'REG' AND (home = ? OR away = ?)",
            (season, week, abbr, abbr),
        )
        return {
            "team": abbr,
            "name": team.name,
            "full_name": team.full_name,
            "conference": team.conference,
            "division": team.division,
            "color": team.color,
            "record": records_before(season, week + 1).get(abbr, ""),
            "rank": cut.get("rank"),
            "power": cut.get("power", rating.get("power")),
            "pythagorean": rating.get("pythagorean"),
            "on_bye": this_week is None,
            "this_week": _game_line(this_week, abbr) if this_week else None,
            "next": _next_opponents(season, week, [abbr]).get(abbr),
            **{
                key: projection.get(key)
                for key in ("playoff_prob", "division_prob", "bye_prob",
                            "sb_prob", "exp_wins", "wins_p10", "wins_p90")
            },
        }

    @app.get("/api/playoffs")
    def playoffs(season: int | None = None) -> dict:
        """The playoff picture: both conferences seeded, and the bracket.

        Seeded from results rather than from the simulator, which answers a
        different question -- twenty thousand hypothetical seasons is where
        the odds come from, and a bracket has to be drawn from the one that
        is actually happening.
        """
        from . import standings as standings_module

        season = season or pipeline.season()
        reg = db.query(
            "SELECT game_id, week, home, away, home_score, away_score, status "
            "FROM games WHERE season = ? AND season_type = 'REG'", (season,))
        post = db.query(
            "SELECT game_id, week, home, away, home_score, away_score, status,"
            " kickoff FROM games WHERE season = ? AND season_type = 'POST' "
            "ORDER BY week, kickoff", (season,))

        picture = standings_module.picture(reg)
        # Playoff odds come from the simulation, and belong beside a seeding
        # taken from results: one says where a team is, the other how likely
        # it is to stay there.
        projections = latest_team_rows(season, "season_projections")
        for rows in picture["conferences"].values():
            for row in rows:
                proj = projections.get(row["team"]) or {}
                row["playoff_prob"] = proj.get("playoff_prob")
                row["name"] = (TEAMS[row["team"]].name
                               if row["team"] in TEAMS else row["team"])
        return {
            "season": season,
            "week": pipeline.current_week(season),
            "conferences": picture["conferences"],
            "legend": picture["legend"],
            "bracket": standings_module.bracket(post, picture["conferences"]),
            "has_postseason": bool(post),
        }

    @app.get("/api/power/history")
    def power_history(season: int | None = None, week: int | None = None) -> dict:
        """The power ranking as it stood in a given week, and how it moved.

        Ranked by the rating itself rather than by projected finish -- see
        Pipeline.store_power_snapshot for why the history has to use one rule
        for every week even though the Teams page uses the other.
        """
        season = season or pipeline.season()
        weeks = [r["week"] for r in db.query(
            "SELECT DISTINCT week FROM power_snapshots WHERE season = ? ORDER BY week",
            (season,),
        )]
        if not weeks:
            return {"season": season, "week": None, "weeks": [],
                    "teams": [], "sources": {}}
        # The current week by default, not the newest row in the table. The
        # Teams page shows the current week's cut, and this is what fills its
        # Move column -- so if this answered about a different week the arrows
        # would describe a table nobody was looking at.
        if week is None:
            current = pipeline.current_week(season)
            week = current if current in weeks else weeks[-1]
        # A week that has not been cut has no movement, and saying so is the
        # whole answer.
        #
        # This used to quietly substitute the newest week it did hold, which is
        # how choosing week twelve in October produced a full set of arrows:
        # they were week six's, under week twelve's heading, describing a
        # fortnight of results that had not happened. A ranking is frozen at
        # the moment the week before it finished -- so until that Monday night
        # game goes final there is no cut for the week, nothing to compare, and
        # nothing an arrow could honestly point at.
        # Which weeks were cut while the app was running for them.
        live_cuts = {
            r["week"]: True for r in db.query(
                "SELECT DISTINCT week FROM power_snapshots "
                "WHERE season = ? AND source = 'live'", (season,),
            )
        }
        # A rebuilt week is not a ranking, so it is not shown as one.
        #
        # The distinction already governed what a week could be compared
        # *against*; it has to govern what a week *is*, too. A rebuilt snapshot
        # is this code's reconstruction of a week nobody was running for, made
        # afterwards from the games that had finished by then -- fine for a
        # trend line, and not a cut that was ever taken. Treating it as one is
        # how week three showed a full column of arrows against a week two
        # that was also a reconstruction: two backfills differenced, presented
        # as a fortnight of movement.
        # Which weeks are a record and which are a reconstruction. Answered
        # whether or not the week asked about has a ranking: it describes the
        # history, not the request, and a caller that cannot see it has no way
        # to tell a backfilled season from a recorded one.
        sources = {r["week"]: r["source"] for r in db.query(
            "SELECT week, MIN(source) AS source FROM power_snapshots "
            "WHERE season = ? GROUP BY week", (season,),
        )}
        if week not in weeks or not live_cuts.get(week):
            never_ranked = week in weeks          # present, but only rebuilt
            return {
                "season": season, "week": week, "weeks": weeks,
                "compared_to": None, "teams": [], "sources": sources,
                "ranked": False,
                "note": (
                    "There is no week one ranking: nothing has been played "
                    "yet, so there is nothing to rank on."
                    if week is not None and week < 2 else
                    f"Week {week} was never ranked live -- this copy of the app "
                    "was not running for it, so what history exists is a "
                    "reconstruction rather than a cut that was taken at the "
                    "time."
                    if never_ranked else
                    f"No ranking has been taken for week {week} yet. A week's "
                    "cut is frozen the moment the week before it finishes, so "
                    f"this fills in once week {week - 1}'s last game is final."
                ),
            }

        rows = db.query(
            "SELECT team, rank, power, elo, pythagorean, wins, losses, ties,"
            " source, captured_at FROM power_snapshots "
            "WHERE season = ? AND week = ? ORDER BY rank",
            (season, week),
        )
        # Movement against the previous week we actually *ranked*, which is
        # not always week-1: a gap in the history would otherwise be reported
        # as a week of dramatic movement that never happened.
        #
        # And only against a live cut. A rebuilt week is this code's opinion of
        # a week nobody was running for, reconstructed afterwards from the
        # games that had finished by then -- useful for a trend line, and not a
        # thing a team can have moved against. An app first opened in week two
        # was showing every team up or down against a week one that had never
        # been on screen; those arrows described a backfill, not a season.
        earlier = [w for w in weeks if w < week and live_cuts.get(w)]
        previous = {
            r["team"]: r["rank"] for r in (
                db.query("SELECT team, rank FROM power_snapshots "
                         "WHERE season = ? AND week = ?", (season, earlier[-1]))
                if earlier else []
            )
        }
        for row in rows:
            team = TEAMS.get(row["team"])
            row["name"] = team.full_name if team else row["team"]
            row["color"] = team.color if team else None
            was = previous.get(row["team"])
            row["previous_rank"] = was
            # Positive means it climbed, which is the direction a reader
            # expects an arrow to point even though the number went down.
            row["move"] = None if was is None else was - row["rank"]
        return {
            "season": season,
            "week": week,
            "weeks": weeks,
            "compared_to": earlier[-1] if earlier else None,
            "ranked": True,
            "teams": rows,
            # Shown rather than smoothed over: they are not the same claim.
            "sources": sources,
        }

    # ------------------------------------------------------------ picks
    def survivor_elimination(season: int) -> dict | None:
        """The week the picked survivor run went out, if it has."""
        from .picks.survivor import USED_WEEKS_KEY, elimination

        used_weeks = db.get_meta(USED_WEEKS_KEY, {}) or {}
        if not used_weeks:
            return None
        games = db.query(
            "SELECT week, home, away, home_score, away_score, status FROM games "
            "WHERE season = ? AND season_type = 'REG'", (season,),
        )
        return elimination(used_weeks, games)

    @app.get("/api/picks")
    def picks(week: int | None = None, season: int | None = None) -> dict:
        season = season or pipeline.season()
        week = week or pipeline.current_week(season)
        pickem = latest_pick("pickem", season, week)
        survivor = latest_pick("survivor", season, week)

        # Any week, not only the one the season is on.
        #
        # Both boards are recorded when they are computed, and they were only
        # ever computed for the current week -- so moving the selector to week
        # 3 or week 12 found no row and drew an empty page. Anything missing is
        # built on demand from the predictions already stored, which is a query
        # and a solve rather than a recompute.
        if pickem is None or survivor is None:
            built = pipeline.picks_for_week(season, week)
            pickem = pickem if pickem is not None else built.get("pickem")
            survivor = survivor if survivor is not None else built.get("survivor")
        return {
            "season": season,
            "week": week,
            "ats": latest_pick("ats", season, week),
            "prediction_markets": latest_pick("prediction_markets", season, week),
            "pickem": pickem,
            "survivor": survivor,
            "survivor_used": db.get_meta("survivor_used_teams", []) or [],
            # Whether the picked run is already dead, and on which week.
            # Derived here rather than stored, so changing the pick that lost
            # brings the plan straight back -- see picks.survivor.elimination.
            "survivor_out": survivor_elimination(season),
        }

    @app.post("/api/survivor/pick")
    def set_survivor_pick(payload: dict[str, Any]) -> dict:
        """Take, or release, this week's survivor team.

        The pick is made on the board now, beside the game it is a bet on,
        rather than on a grid of thirty-two crests on another page. That grid
        recorded a set -- "I used KC" -- and a set cannot be walked against a
        schedule: it does not say whether that was the week they were fourteen
        point favourites or the week they lost. Choosing it where the week is
        already on screen means the week comes with it for free.

        The two older keys are still written from this one, so the planner and
        the tracker keep reading what they have always read.
        """
        season = int(payload.get("season") or pipeline.season())
        week = int(payload.get("week") or pipeline.current_week(season))
        team = (payload.get("team") or "").strip().upper() or None
        if team and team not in TEAMS:
            raise HTTPException(status_code=400, detail=f"unknown team: {team}")

        by_week = dict(db.get_meta(SURVIVOR_PICKS_KEY, {}) or {})
        season_picks = dict(by_week.get(str(season), {}) or {})
        if team:
            # One team, one season. Taking a team you already spent moves the
            # pick rather than keeping both, because keeping both is not a
            # thing survivor allows and silently doing it would show a run
            # that cannot happen.
            for other_week, other in list(season_picks.items()):
                if other == team and other_week != str(week):
                    season_picks.pop(other_week)
            season_picks[str(week)] = team
        else:
            season_picks.pop(str(week), None)
        by_week[str(season)] = season_picks
        db.set_meta(SURVIVOR_PICKS_KEY, by_week)
        _sync_legacy_survivor_keys(season)
        # Only the plan, not the world. This used to force a full recompute --
        # Elo over every game ever, the model over the whole history, twenty
        # thousand season simulations -- none of which depends on which team
        # you spent. See Pipeline.replan_survivor.
        pipeline.replan_survivor(season)
        return {"ok": True, "season": season, "week": week, "team": team,
                "picks": season_picks}

    @app.post("/api/survivor/used")
    def set_survivor_used(payload: dict[str, Any]) -> dict:
        from .picks.survivor import USED_WEEKS_KEY

        teams_used = [str(t).upper() for t in (payload.get("teams") or []) if t]
        unknown = [t for t in teams_used if t not in TEAMS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown teams: {unknown}")

        # Which week each team was spent in. The list alone is a set, and a set
        # cannot be walked against a schedule: "I used KC" does not say whether
        # that was the week they were favoured by fourteen or the week they
        # lost. Newly marked teams are stamped with the week on screen; a team
        # released has its stamp dropped with it.
        season = pipeline.season()
        week = int(payload.get("week") or pipeline.current_week(season))
        stamps = dict(db.get_meta(USED_WEEKS_KEY, {}) or {})
        for team in teams_used:
            stamps.setdefault(team, week)
        for team in list(stamps):
            if team not in teams_used:
                stamps.pop(team)

        db.set_meta("survivor_used_teams", teams_used)
        db.set_meta(USED_WEEKS_KEY, stamps)
        pipeline.recompute()
        return {"teams": teams_used, "weeks": stamps, "ok": True}

    @app.get("/api/survivor/tracker")
    def survivor_tracker(season: int | None = None) -> dict:
        """The run as first planned against the run actually picked."""
        from .picks.survivor import ORIGINAL_KEY, USED_WEEKS_KEY, track

        season = season or pipeline.season()
        original = db.get_meta(f"{ORIGINAL_KEY}:{season}", {}) or {}
        games = db.query(
            "SELECT week, home, away, home_score, away_score, status FROM games "
            "WHERE season = ? AND season_type = 'REG'", (season,),
        )
        used_weeks = db.get_meta(USED_WEEKS_KEY, {}) or {}
        out = track(original.get("path") or [], used_weeks, games)
        out["season"] = season
        out["saved_at"] = original.get("saved_at")
        out["from_week"] = original.get("from_week")

        # What the original run would do from here, had it survived.
        #
        # Once the plan busts, its column stops being a rival and becomes a
        # gravestone -- a list of weeks with a red mark partway down and
        # nothing after it. The interesting question does not die with it:
        # given where the season actually is, and given the teams *you* have
        # spent, what would the optimiser pick next? So the counterfactual is
        # planned from the current week with your used list, which is the
        # constraint that genuinely binds -- a team you have burned is gone
        # whatever an imaginary run would like to do with it.
        if out["original"]["out_week"] is not None:
            with contextlib.suppress(Exception):
                built = pipeline.picks_for_week(
                    season, pipeline.current_week(season),
                    used_teams=sorted(used_weeks))
                out["original"]["continuation"] = built.get("survivor")
        return out

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

    @app.post("/api/settings/test-prediction-markets")
    def test_prediction_markets() -> dict:
        from . import settings as settings_module

        return settings_module.test_prediction_markets()

    @app.post("/api/settings/test-odds-key")
    def test_odds_key(payload: dict) -> dict:
        from . import settings as settings_module

        return settings_module.validate_odds_key(payload.get("key") or "")

    # -------------------------------------------------------- assistant
    @app.get("/api/assistant/status")
    def assistant_status() -> dict:
        from . import assistant

        return assistant.status()

    # Installing and running the model server, from the app rather than from a
    # terminal the buyer was never going to open.
    @app.get("/api/assistant/setup")
    def assistant_setup_state() -> dict:
        from . import localmodel

        return localmodel.state()

    @app.post("/api/assistant/setup")
    def assistant_setup_start(payload: dict | None = None) -> dict:
        """Start the download. Returns immediately: this takes minutes, and a
        request that holds a connection open for minutes is a request that
        times out somewhere between here and the page."""
        from . import localmodel

        return localmodel.begin((payload or {}).get("model"))

    @app.get("/api/assistant/chats")
    def assistant_chats() -> dict:
        from . import assistant

        return {"chats": assistant.list_chats()}

    @app.post("/api/assistant/chats")
    def assistant_new_chat(payload: dict | None = None) -> dict:
        from . import assistant

        return assistant.create_chat((payload or {}).get("title") or "New chat")

    @app.get("/api/assistant/chats/{chat_id}")
    def assistant_chat(chat_id: str) -> dict:
        from . import assistant

        return {"id": chat_id, "messages": assistant.chat_messages(chat_id)}

    @app.patch("/api/assistant/chats/{chat_id}")
    def assistant_rename_chat(chat_id: str, payload: dict) -> dict:
        from . import assistant

        try:
            return assistant.rename_chat(chat_id, payload.get("title") or "")
        except assistant.AssistantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/assistant/chats/{chat_id}")
    def assistant_delete_chat(chat_id: str) -> dict:
        from . import assistant

        return assistant.delete_chat(chat_id)

    @app.post("/api/assistant/ask")
    def assistant_ask(payload: dict) -> dict:
        """Ask within a conversation.

        The chat is the unit, not the request: the transcript is read from and
        written to the database, so a reload, a restart or a different window
        all continue the same thread rather than starting a fresh one that only
        looks continuous.
        """
        from . import assistant

        question = str(payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="expected a question")
        season = int(payload.get("season") or pipeline.season())
        week = int(payload.get("week") or pipeline.current_week(season))

        chat_id = payload.get("chat_id")
        if not chat_id:
            chat_id = assistant.create_chat(assistant.title_from(question))["id"]
        elif not assistant.chat_messages(chat_id):
            # First question in an untitled chat names it.
            with contextlib.suppress(Exception):
                assistant.rename_chat(chat_id, assistant.title_from(question))

        assistant.append_message(chat_id, "user", question)
        history = assistant.chat_messages(chat_id)
        try:
            result = assistant.ask(history, season, week)
        except assistant.AssistantError as exc:
            # The question stays in the transcript. Losing what you asked
            # because the model could not answer it is its own small insult,
            # and you may want to retry it verbatim.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        assistant.append_message(chat_id, "assistant", result["reply"])
        return {**result, "chat_id": chat_id,
                "messages": assistant.chat_messages(chat_id)}

    @app.post("/api/assistant/ask/stream")
    def assistant_ask_stream(payload: dict):
        """The same question, answered as it is written.

        The total wait is generation on the user's own machine and nothing
        here shortens it. What this changes is that the wait is spent reading
        rather than watching: the first token lands in about a second and the
        rest arrives at something close to reading speed.

        Server-sent events, because the payload is one-way and text -- a
        WebSocket would be a second protocol for no gain. Each line is one
        JSON object: `{"delta": "..."}` while it writes, then a final
        `{"done": true, ...}` or `{"error": "..."}`.

        The transcript is written here rather than in the generator so that
        an answer is stored exactly once, whether the stream finished or the
        reader went away mid-sentence.
        """
        from . import assistant

        question = str(payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="expected a question")
        season = int(payload.get("season") or pipeline.season())
        week = int(payload.get("week") or pipeline.current_week(season))

        chat_id = payload.get("chat_id")
        if not chat_id:
            chat_id = assistant.create_chat(assistant.title_from(question))["id"]
        elif not assistant.chat_messages(chat_id):
            with contextlib.suppress(Exception):
                assistant.rename_chat(chat_id, assistant.title_from(question))

        assistant.append_message(chat_id, "user", question)
        history = assistant.chat_messages(chat_id)

        def events():
            yield _sse({"chat_id": chat_id})
            stored = False
            try:
                for event in assistant.stream(history, season, week):
                    if event.get("done"):
                        assistant.append_message(
                            chat_id, "assistant", event["reply"])
                        stored = True
                        yield _sse({**event, "chat_id": chat_id})
                    else:
                        yield _sse(event)
            except Exception as exc:                          # noqa: BLE001
                # Streaming is the fast path, not the only one. Anything that
                # is not Ollama, and anything that broke mid-stream before a
                # word arrived, falls back to the blocking call rather than
                # showing the reader an error they can do nothing about.
                if stored:
                    yield _sse({"error": str(exc)})
                    return
                try:
                    result = assistant.ask(history, season, week)
                except assistant.AssistantError as fallback_exc:
                    yield _sse({"error": str(fallback_exc)})
                    return
                assistant.append_message(chat_id, "assistant", result["reply"])
                yield _sse({"delta": result["reply"]})
                yield _sse({"done": True, "reply": result["reply"],
                            "model": result.get("model", ""),
                            "chat_id": chat_id})

        return StreamingResponse(
            events(), media_type="text/event-stream",
            # Nothing between here and the browser should be holding this in a
            # buffer: the whole point is that the first token arrives early.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
                # The most recently *written* row, not the one carrying the
                # latest date. Those differ: a feed stamps its rows with its
                # own clock, while a player who has cleared the report is
                # marked healthy with ours, because a feed that has stopped
                # mentioning him supplies no date at all. Ordering by the date
                # therefore let a stale "Active" outrank a later listing, and
                # the player read as healthy while actually being out.
                "SELECT i.team, i.player, i.position, i.status, i.detail,"
                " i.injury, i.return_date, i.first_seen, i.updated_at "
                "FROM injuries i JOIN (SELECT team, player, MAX(id) AS m "
                "  FROM injuries GROUP BY team, player) x "
                "  ON x.team = i.team AND x.player = i.player AND x.m = i.id "
                "ORDER BY i.team, i.player"
            )
            if _is_notable_injury(r.get("status"))
        ]
        for row in injuries:
            row["how_long"] = _how_long(row)
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


# How long a player has been unavailable, and for how much longer.
#
# The report answers "how long" in two directions and the feed supplies each
# only sometimes, so this prefers the forward-looking answer and falls back to
# the backward-looking one rather than printing a dash. An expected return is
# what actually changes a decision; time served is what is always knowable.
_LONG_TERM = ("injured reserve", "ir", "physically unable", "pup",
              "non football", "nfi", "suspended")


def _how_long(row: dict) -> str | None:
    status = (row.get("status") or "").strip().lower()
    back = None
    first = row.get("first_seen")
    if first:
        days = _days_since(first)
        if days is not None:
            if days < 6:
                back = "this week"
            else:
                weeks = max(1, round(days / 7))
                back = f"{weeks} week{'' if weeks == 1 else 's'}"

    ahead = None
    if row.get("return_date"):
        days = _days_until(row["return_date"])
        if days is not None and days > 0:
            weeks = round(days / 7)
            ahead = "back this week" if weeks < 1 else (
                f"back in ~{weeks} week{'' if weeks == 1 else 's'}")
        elif days is not None:
            ahead = "due back"
    elif any(k in status for k in _LONG_TERM):
        # No date, but the status itself is a duration: these designations
        # carry a minimum absence in the rules, so saying "this week" would be
        # wrong in a way the reader would act on.
        ahead = "multi-week"

    # One fact, not two. The panel is half a screen wide and the status badge
    # is what a reader scans for, so a column carrying both "3 weeks" and "back
    # in ~2 weeks" pushed the badge off the edge entirely. When a return is
    # known it is also the more useful of the pair -- time served is history,
    # time remaining is the thing that changes a decision.
    return ahead or back


def _days_since(stamp: str) -> float | None:
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400.0)


def _days_until(stamp: str) -> float | None:
    days = _days_since(stamp)
    if days is None:
        return None
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (then - datetime.now(timezone.utc)).total_seconds() / 86400.0


# Moved to nflpicker.availability so the pipeline can apply the same test when
# it decides a player's spell on the report has begun. Kept as a name here
# because it reads as a local predicate at every call site.
_is_notable_injury = is_notable_injury


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


# What decides the power ranking's order.
#
# It used to be the rating, with projected wins as a tie-break. The rating is
# mostly shrunk Elo, Elo moves on results, and every other term in it is faded
# in by games played -- so through September the order was very nearly a
# standings table with extra steps, and a 1-0 team sat above an 0-1 team that
# was plainly better.
#
# So the order is a blend of the measures that are about how good a team is
# rather than how its Sundays have gone. Record is not one of the inputs.
# Projected wins carries the games already banked, which is the only way
# results enter at all, and it is the schedule-adjusted season outlook that
# makes it worth carrying.
#
# `power` here is still the rating the projections and the model are built
# from. Nothing below changes a prediction; it changes the order of a table.
def _pythagorean(rating: dict, points: list[float] | None) -> float | None:
    """The stored Pythagorean, or one worked out from the season's scores.

    A team with no finished games has no points either way, and there is no
    Pythagorean to have: that is the one case this returns nothing for, and the
    card says so instead of showing a dash that could mean anything.
    """
    stored = rating.get("pythagorean")
    if stored is not None:
        return stored
    if not points:
        return None
    from .ratings.power import pythagorean_expectation

    return pythagorean_expectation(points[0], points[1])


# One set, and projected finish carries it.
#
# This has been wrong twice in opposite directions, so it is worth saying what
# the column is for. The Teams page shows PROJ beside every team -- the wins a
# team is expected to finish with, out of twenty thousand simulated seasons --
# and the order has to agree with it, because a table that ranks the Rams 28th
# next to their own projection of 10.2 wins is not expressing a view, it is
# contradicting itself on screen.
#
# Projected wins is also simply the best answer to the question the page is
# asked: who beats whom. It is the rating applied to a real remaining
# schedule, so it already knows the Rams beat the Dolphins and the Chiefs beat
# the Cardinals. The rating stays as a check on it, and the Pythagorean and the
# odds fill in around the edges, but none of them should be able to outvote the
# projection.
#
# What went wrong before: the weight was spread evenly enough that four terms
# which all bank the same Sunday -- projected wins, the floor of the range,
# title odds, and a Pythagorean off one game -- could between them sort the
# league by its win-loss column. Records are frozen to completed weeks now, so
# a single result no longer swings four terms at once, and the fix that split
# the weights early and late is no longer carrying anything.
RANK_WEIGHTS = {
    "exp_wins": 0.50,       # where the season is projected to end up
    "power": 0.20,          # neutral-field strength, as a check on it
    "sb_prob": 0.12,        # what twenty thousand seasons think of them
    "wins_p10": 0.10,       # the floor of the 80% range: a team's bad case
    "pythagorean": 0.08,    # points scored and allowed, which ignores who won
}


def rank_weights(played: float = 0.0) -> dict[str, float]:
    """The blend. `played` is accepted and ignored.

    It used to fade between an early set and a late one. That existed to stop
    one Sunday deciding the table, and the real cause of that was records which
    included games the week had not finished yet -- fixed where it belonged, in
    what the week's cut is allowed to see. Kept as a parameter so callers and
    tests do not have to care which it is.
    """
    return dict(RANK_WEIGHTS)


def _games_played(projections: dict) -> float:
    """Games the average team has played, from the records we already hold.

    One number for the whole table rather than one per team: a team on a bye
    should not be ranked by a different rule from the rest of the league.
    """
    counts = []
    for row in (projections or {}).values():
        if not isinstance(row, dict):
            continue
        played = sum(float(row.get(k) or 0.0)
                     for k in ("wins_actual", "losses_actual", "ties_actual"))
        counts.append(played)
    return sum(counts) / len(counts) if counts else 0.0


def _z(values: dict[str, float | None]) -> dict[str, float]:
    """Standard scores, with a missing value reading as league average.

    Standardising is what lets four quantities on four scales -- wins, a
    percentage, a rating in points, a probability -- be weighed against each
    other at all. Without it the term measured in wins would decide everything
    and the probabilities would be rounding error.
    """
    live = [v for v in values.values() if v is not None]
    if len(live) < 2:
        return {t: 0.0 for t in values}
    mean = sum(live) / len(live)
    variance = sum((v - mean) ** 2 for v in live) / len(live)
    sd = variance ** 0.5
    if sd <= 0:
        return {t: 0.0 for t in values}
    return {t: 0.0 if v is None else (v - mean) / sd for t, v in values.items()}


def ranking_scores(ratings: dict, projections: dict) -> dict[str, float]:
    """One number per team, higher is better, from RANK_WEIGHTS."""
    teams = sorted(set(ratings) | set(projections))

    def field(source: dict, name: str) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for team in teams:
            row = source.get(team)
            value = row.get(name) if isinstance(row, dict) else None
            try:
                out[team] = None if value is None else float(value)
            except (TypeError, ValueError):
                out[team] = None
        return out

    parts = {
        "exp_wins": _z(field(projections, "exp_wins")),
        "wins_p10": _z(field(projections, "wins_p10")),
        "sb_prob": _z(field(projections, "sb_prob")),
        "pythagorean": _z(field(ratings, "pythagorean")),
        "power": _z(field(ratings, "power")),
    }
    weights = rank_weights(_games_played(projections))
    return {team: sum(weights[k] * parts[k][team] for k in weights)
            for team in teams}


def team_rank_order(season: int, ratings: dict | None = None,
                    projections: dict | None = None) -> list[str]:
    """Our teams, best first -- the one ordering the whole app calls "our rank".

    See RANK_WEIGHTS for what decides it. The abbreviation is the final
    tie-break, and it is there on purpose: two teams that score identically
    have to land in the same order on every render, or the table quietly
    reshuffles itself between refreshes and the top of the league looks
    unstable for no reason a reader can see.
    """
    if ratings is None:
        ratings = latest_team_rows(season, "team_ratings")
    if projections is None:
        projections = latest_team_rows(season, "season_projections")
    scores = ranking_scores(ratings, projections)
    return sorted(scores, key=lambda t: (-scores[t], t))


# Week -> team, per season. The two older keys (a flat list of teams, and a
# team -> week map) are derived from this one so nothing downstream had to
# change; this is the one that is written when a pick is made.
SURVIVOR_PICKS_KEY = "survivor_picks_by_week"


def survivor_picks(season: int) -> dict[int, str]:
    raw = (db.get_meta(SURVIVOR_PICKS_KEY, {}) or {}).get(str(season), {}) or {}
    out = {}
    for week, team in raw.items():
        with contextlib.suppress(TypeError, ValueError):
            out[int(week)] = str(team).upper()
    return out


def survivor_pick_for(season: int, week: int) -> str | None:
    return survivor_picks(season).get(int(week))


def _sync_legacy_survivor_keys(season: int) -> None:
    """Rewrite the flat list and the team -> week stamps from the week map."""
    from .picks.survivor import USED_WEEKS_KEY

    picks = survivor_picks(season)
    db.set_meta("survivor_used_teams", sorted(set(picks.values())))
    db.set_meta(USED_WEEKS_KEY, {team: week for week, team in picks.items()})


def records_before(season: int, week: int) -> dict[str, str]:
    """Every team's W-L(-T) going into `week`, as a string ready to print.

    Before, not including: a record beside a game is the record the two teams
    bring to it. Counting the week itself would have week two's board reading
    1-0 for a team whose only game is the one underneath the number.
    """
    tally: dict[str, list[int]] = {}
    for row in db.query(
        "SELECT home, away, home_score, away_score FROM games "
        "WHERE season = ? AND week < ? AND status = 'final' "
        "AND home_score IS NOT NULL AND away_score IS NOT NULL",
        (season, week),
    ):
        home, away = row["home"], row["away"]
        hs, as_ = row["home_score"], row["away_score"]
        for team in (home, away):
            tally.setdefault(team, [0, 0, 0])
        if hs == as_:
            tally[home][2] += 1
            tally[away][2] += 1
        else:
            winner, loser = (home, away) if hs > as_ else (away, home)
            tally[winner][0] += 1
            tally[loser][1] += 1
    out = {}
    for team in TEAMS:
        wins, losses, ties = tally.get(team, [0, 0, 0])
        out[team] = f"{wins}-{losses}" + (f"-{ties}" if ties else "")
    return out


def _setting_is_on(key: str, default: bool = True) -> bool:
    """A boolean setting, read the way the rest of the app writes them."""
    import os

    raw = (os.environ.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def week_options(season: int) -> list[dict]:
    """Every week you can choose, regular season then postseason rounds.

    One value per week, and it is the week: the postseason is stored numbered
    on from the regular season (see espn.REGULAR_SEASON_WEEKS), so nothing has
    to be translated. Only the label changes -- nobody calls the divisional
    round week twenty.
    """
    from . import standings as standings_module

    out = [
        {"value": r["week"], "label": f"Week {r['week']}", "type": "REG",
         "week": r["week"]}
        for r in db.query(
            "SELECT DISTINCT week FROM games WHERE season = ? "
            "AND season_type = 'REG' ORDER BY week", (season,))
    ]
    post = db.query(
        "SELECT week, COUNT(*) AS n FROM games WHERE season = ? "
        "AND season_type = 'POST' GROUP BY week ORDER BY week", (season,))
    if not post:
        return out
    rounds = standings_module.label_rounds(
        [{"week": r["week"]} for r in post for _ in range(r["n"])])
    for row in sorted(post, key=lambda r: r["week"]):
        name = rounds.get(row["week"])
        if name:
            out.append({"value": row["week"], "label": name, "type": "POST",
                        "week": row["week"]})
    return out


def week_kind(season: int, week: int | None) -> str:
    """Whether a week is a regular-season week or a postseason round.

    The week number is the same either way -- the postseason is stored
    numbered on from the regular season -- so this only answers which part of
    the season it belongs to, for the handful of things that differ: there are
    no byes in January, and no survivor pool.
    """
    if week is None:
        return "REG"
    row = db.query_one(
        "SELECT season_type FROM games WHERE season = ? AND week = ? LIMIT 1",
        (season, int(week)),
    )
    return (row or {}).get("season_type") or "REG"


def _game_line(game: dict, team: str) -> dict:
    """One team's view of one game: who, where, and how it went if it has."""
    home = game["home"] == team
    return {
        "game_id": game["game_id"],
        "week": game["week"],
        "opponent": game["away"] if home else game["home"],
        "home": home,
        "kickoff": game["kickoff"],
        "status": game["status"],
        "score": (None if game.get("home_score") is None
                  else f"{int(game['home_score'])}-{int(game['away_score'])}"
                  if home else
                  f"{int(game['away_score'])}-{int(game['home_score'])}"),
    }


def _next_opponents(season: int, week: int,
                    teams: list[str]) -> dict[str, dict]:
    """Each team's next scheduled game after `week`.

    Not simply week + 1: a team can be on bye in the last week the schedule
    reaches, and the back half of the season is loaded in chunks, so "the
    following week" is sometimes a week with no rows in it at all. Searching
    forward answers the question that was actually asked -- who is next --
    and returns nothing rather than a wrong opponent when the schedule does
    not go that far.
    """
    if not teams:
        return {}
    wanted = set(teams)
    out: dict[str, dict] = {}
    for row in db.query(
        "SELECT week, home, away, kickoff FROM games "
        "WHERE season = ? AND week > ? AND season_type = 'REG' "
        "ORDER BY week, kickoff", (season, week),
    ):
        for side, other in (("home", "away"), ("away", "home")):
            team = row[side]
            if team in wanted and team not in out:
                out[team] = {
                    "week": row["week"],
                    "opponent": row[other],
                    "home": side == "home",
                    "kickoff": row["kickoff"],
                }
        if len(out) == len(wanted):
            break
    return out


def teams_on_bye(season: int, week: int) -> list[str]:
    """Who has no game this week. Empty before the schedule reaches the
    weeks that have byes in them, and empty again in the weeks that do not."""
    playing = set()
    for row in db.query(
        "SELECT home, away FROM games WHERE season = ? AND week = ? "
        "AND season_type = 'REG'", (season, week),
    ):
        playing.add(row["home"])
        playing.add(row["away"])
    if not playing:
        return []
    return sorted(t for t in TEAMS if t not in playing)


def game_cards(season: int, week: int, season_type: str = "REG") -> list[dict]:
    """Everything the UI needs to render one week's games.

    Scoped to a season type as well as a week, because the postseason starts
    its own numbering again at one: without it, asking for week 1 in January
    returned the opening Sunday of September alongside the wild-card round.
    """
    games = db.query(
        "SELECT * FROM games WHERE season = ? AND week = ? AND season_type = ? "
        "ORDER BY kickoff, game_id",
        (season, week, season_type),
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

    # Each team's record going into this week, which is what a record on a
    # game card means: week two's board says 1-0, not what they finished at.
    records = records_before(season, week)

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
                "home_record": records.get(game["home"], ""),
                "away_record": records.get(game["away"], ""),
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
