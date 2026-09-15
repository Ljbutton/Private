"""Refresh orchestration: fetch, store, recompute.

Each refresh stage is independent and failure-isolated.  A dead RSS feed must
not stop odds from updating, and an exhausted Odds API quota must not stop
scores from coming in.  Anything that fails is recorded in ``fetch_log`` and
surfaced in the UI as a source-health row rather than thrown away.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from . import db
from .config import get_config
from .market.consensus import build_consensus
from .ml.features import build_features
from .ml.predict import Predictor
from .news.impact import tag_items
from .picks.edges import find_edges
from .picks.pickem import build_pickem
from .picks.survivor import plan_survivor
from .ratings.efficiency import from_epa_frame
from .ratings.elo import run_elo
from .ratings.power import build_power_ratings
from .sim.season import simulate_season
from .sources.base import SourceError
from .util import (
    american_to_prob,
    current_season,
    devig,
    estimate_week,
    now_iso,
    to_utc,
)

# How many complete seasons of synthetic history demo mode loads behind the
# current one.  Five is enough for Elo to converge and for the trainer to have
# a few thousand games.
DEMO_HISTORY_SEASONS = 5

GAME_COLUMNS = [
    "game_id", "season", "week", "season_type", "kickoff", "home", "away",
    "home_score", "away_score", "status", "neutral_site", "roof", "venue", "updated_at",
]


@dataclass
class RefreshResult:
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    stages: dict[str, dict] = field(default_factory=dict)

    def record(self, stage: str, ok: bool, detail: str = "", **extra) -> None:
        self.stages[stage] = {"ok": ok, "detail": detail, "at": now_iso(), **extra}

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stages": self.stages,
            "ok": all(s["ok"] for s in self.stages.values()) if self.stages else False,
        }


class Pipeline:
    def __init__(self, *, demo: bool | None = None) -> None:
        self.config = get_config()
        self.demo = self.config.demo if demo is None else demo
        self._predictor: Predictor | None = None

    # ------------------------------------------------------------------ util
    @property
    def predictor(self) -> Predictor:
        if self._predictor is None:
            self._predictor = Predictor()
        return self._predictor

    def reload_model(self) -> None:
        self._predictor = None

    def season(self) -> int:
        if self.config.season_override:
            return self.config.season_override
        row = db.query_one("SELECT MAX(season) AS s FROM games")
        return int(row["s"]) if row and row["s"] else current_season()

    def current_week(self, season: int | None = None) -> int:
        """The week to show: the earliest week that still has an unplayed game."""
        season = season or self.season()
        row = db.query_one(
            "SELECT MIN(week) AS w FROM games WHERE season = ? AND season_type = 'REG' "
            "AND status != 'final'",
            (season,),
        )
        if row and row["w"]:
            return int(row["w"])
        row = db.query_one(
            "SELECT MAX(week) AS w FROM games WHERE season = ? AND season_type = 'REG'",
            (season,),
        )
        return int(row["w"]) if row and row["w"] else estimate_week()

    # ------------------------------------------------------------- storage
    def upsert_games(self, games: list[dict]) -> int:
        rows = []
        stamp = now_iso()
        for g in games:
            if not g.get("game_id") or not g.get("home") or not g.get("away"):
                continue
            rows.append([
                str(g["game_id"]), int(g.get("season") or 0), int(g.get("week") or 0),
                g.get("season_type") or "REG", g.get("kickoff"), g["home"], g["away"],
                g.get("home_score"), g.get("away_score"), g.get("status") or "scheduled",
                int(bool(g.get("neutral_site"))), g.get("roof"), g.get("venue"), stamp,
            ])
        if rows:
            db.executemany(
                f"INSERT INTO games({','.join(GAME_COLUMNS)}) "
                f"VALUES({','.join('?' for _ in GAME_COLUMNS)}) "
                "ON CONFLICT(game_id) DO UPDATE SET "
                "season=excluded.season, week=excluded.week, season_type=excluded.season_type, "
                "kickoff=excluded.kickoff, home_score=excluded.home_score, "
                "away_score=excluded.away_score, status=excluded.status, "
                "neutral_site=excluded.neutral_site, roof=excluded.roof, "
                "venue=excluded.venue, updated_at=excluded.updated_at",
                rows,
            )
        return len(rows)

    def _game_index(self, season: int | None = None) -> dict[tuple[str, str], list[dict]]:
        """Index games by (home, away) so odds events can be matched to them."""
        params: list[Any] = []
        sql = "SELECT game_id, kickoff, home, away, season, week FROM games"
        if season:
            sql += " WHERE season = ?"
            params.append(season)
        index: dict[tuple[str, str], list[dict]] = {}
        for row in db.query(sql, params):
            index.setdefault((row["home"], row["away"]), []).append(row)
        return index

    def match_quotes(self, quotes: list[dict]) -> tuple[list[dict], int]:
        """Attach our game_id to each odds quote; drop those we cannot place.

        Odds feeds identify games by team names and kickoff, never by our id, so
        matching is on (home, away) plus a kickoff within two days — enough to
        disambiguate a home-and-home without being brittle about time zones.
        """
        index = self._game_index()
        matched: list[dict] = []
        unmatched = 0
        for q in quotes:
            candidates = index.get((q.get("home"), q.get("away")))
            if not candidates:
                unmatched += 1
                continue
            kickoff = to_utc(q.get("kickoff"))
            best = None
            if kickoff and len(candidates) > 1:
                scored = []
                for c in candidates:
                    ck = to_utc(c["kickoff"])
                    if ck:
                        scored.append((abs((ck - kickoff).total_seconds()), c))
                if scored:
                    scored.sort(key=lambda pair: pair[0])
                    if scored[0][0] <= 2 * 86400:
                        best = scored[0][1]
            else:
                best = candidates[0]
            if best is None:
                unmatched += 1
                continue
            matched.append({**q, "game_id": best["game_id"]})
        return matched, unmatched

    def store_quotes(self, quotes: list[dict]) -> int:
        stamp = now_iso()
        rows = []
        for q in quotes:
            if not q.get("game_id"):
                continue
            rows.append([
                q["game_id"], q.get("book") or "unknown", q.get("market") or "spread",
                q.get("captured_at") or stamp,
                q.get("home_point"), q.get("away_point"),
                q.get("home_price"), q.get("away_price"),
            ])
        if rows:
            db.executemany(
                "INSERT OR IGNORE INTO odds_snapshots"
                "(game_id, book, market, captured_at, home_point, away_point, home_price, away_price) "
                "VALUES(?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def rebuild_consensus(self, game_ids: list[str] | None = None, *,
                          full_history: bool = False) -> int:
        """Recompute the cross-book consensus for the given games.

        By default this writes one snapshot per call, which is what a live poll
        wants.  With ``full_history`` it instead replays the stored quotes and
        writes a consensus *as of* every distinct timestamp in them.  That is
        what makes the movement chart useful immediately: books report their own
        ``last_update``, so a single fetch already carries history, and without
        the replay the chart would stay flat until the app had been running for
        days.
        """
        stamp = now_iso()
        if game_ids is None:
            # Upcoming games always get a fresh snapshot. Final games are included
            # only when they have never had one, so a first run backfills history
            # without rewriting closing lines on every poll afterwards.
            rows = db.query(
                "SELECT DISTINCT o.game_id FROM odds_snapshots o "
                "JOIN games g ON g.game_id = o.game_id "
                "WHERE g.status != 'final' OR NOT EXISTS "
                "  (SELECT 1 FROM consensus c WHERE c.game_id = o.game_id)"
            )
            game_ids = [r["game_id"] for r in rows]

        written = 0
        payload: list[list] = []
        for game_id in game_ids:
            quotes = db.query(
                "SELECT book, market, captured_at, home_point, away_point, home_price, away_price "
                "FROM odds_snapshots WHERE game_id = ? ORDER BY captured_at",
                (game_id,),
            )
            if not quotes:
                continue
            is_final = bool(db.query_one(
                "SELECT 1 AS x FROM games WHERE game_id = ? AND status = 'final'", (game_id,)
            ))

            if full_history:
                # One consensus per distinct quote time, each built only from
                # quotes at or before that moment — the market as it then stood.
                times = sorted({q["captured_at"] for q in quotes if q["captured_at"]})
                snapshots = [
                    (t, [q for q in quotes if (q["captured_at"] or "") <= t]) for t in times
                ]
            else:
                captured = (quotes[-1]["captured_at"] if is_final else stamp) or stamp
                snapshots = [(captured, quotes)]

            for captured, visible in snapshots:
                consensus = build_consensus(game_id, visible, captured)
                if consensus is None:
                    continue
                row = consensus.to_row()
                payload.append([row[k] for k in (
                    "game_id", "captured_at", "spread_home", "spread_price_home",
                    "spread_price_away", "total_points", "total_price_over",
                    "total_price_under", "ml_home", "ml_away", "home_win_prob",
                    "n_books", "books")])
                written += 1

        db.executemany(
            "INSERT OR REPLACE INTO consensus"
            "(game_id, captured_at, spread_home, spread_price_home, spread_price_away,"
            " total_points, total_price_over, total_price_under, ml_home, ml_away,"
            " home_win_prob, n_books, books) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
        return written

    def latest_consensus(self, season: int | None = None) -> dict[str, dict]:
        """Most recent consensus row per game."""
        sql = (
            "SELECT c.* FROM consensus c "
            "JOIN (SELECT game_id, MAX(captured_at) AS m FROM consensus GROUP BY game_id) x "
            "ON x.game_id = c.game_id AND x.m = c.captured_at"
        )
        params: list[Any] = []
        if season:
            sql += " JOIN games g ON g.game_id = c.game_id WHERE g.season = ?"
            params.append(season)
        return {r["game_id"]: r for r in db.query(sql, params)}

    # ------------------------------------------------------------ fetch stages
    def refresh_schedule(self, result: RefreshResult) -> None:
        start = time.monotonic()
        try:
            if self.demo:
                season = self.config.season_override or current_season()
                week = self.config.demo_week or estimate_week(season=season)
                # Load several complete prior seasons, not just the current one.
                # Ratings built from a single season are barely better than their
                # priors, and the trainer needs a few thousand games before its
                # walk-forward evaluation means anything.
                count = 0
                for past in range(season - DEMO_HISTORY_SEASONS, season):
                    count += self.upsert_games(_demo_season(past, 18)["games"])
                payload = _demo_season(season, max(0, week - 1))
                count += self.upsert_games(payload["games"])
                detail = (
                    f"demo seasons {season - DEMO_HISTORY_SEASONS}-{season}, {count} games"
                )
            else:
                from .sources.espn import EspnSource

                season = self.season()
                espn = EspnSource()
                games = espn.season_schedule(season)
                if not games:
                    raise SourceError("ESPN returned no games")
                count = self.upsert_games(games)
                detail = f"{count} games from ESPN"
                self._store_espn_fallback_odds(games)
            result.record("schedule", True, detail, count=count)
            db.log_fetch("schedule", True, detail, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001
            result.record("schedule", False, str(exc))
            db.log_fetch("schedule", False, str(exc), int((time.monotonic() - start) * 1000))

    def _store_espn_fallback_odds(self, games: list[dict]) -> None:
        """ESPN posts one consensus line; without an Odds API key it is all we have."""
        if self.config.has_odds_key:
            return
        stamp = now_iso()
        quotes: list[dict] = []
        for g in games:
            odds = g.get("espn_odds")
            if not odds:
                continue
            base = {"game_id": g["game_id"], "book": "espn-consensus", "captured_at": stamp}
            if odds.get("spread_home") is not None:
                quotes.append({**base, "market": "spread",
                               "home_point": odds["spread_home"],
                               "away_point": -odds["spread_home"],
                               "home_price": -110, "away_price": -110})
            if odds.get("total") is not None:
                quotes.append({**base, "market": "total", "home_point": odds["total"],
                               "away_point": odds["total"],
                               "home_price": -110, "away_price": -110})
            if odds.get("ml_home") is not None and odds.get("ml_away") is not None:
                quotes.append({**base, "market": "moneyline",
                               "home_price": int(odds["ml_home"]),
                               "away_price": int(odds["ml_away"])})
        if quotes:
            self.store_quotes(quotes)

    def refresh_odds(self, result: RefreshResult, *, force: bool = False) -> None:
        start = time.monotonic()
        try:
            if self.demo:
                season = self.config.season_override or current_season()
                quotes: list[dict] = []
                for past in range(season - DEMO_HISTORY_SEASONS, season):
                    quotes.extend(_demo_season(past, 18)["quotes"])
                week = self.config.demo_week or estimate_week(season=season)
                quotes.extend(_demo_season(season, max(0, week - 1))["quotes"])
                matched, unmatched = self.match_quotes(quotes)
                stored = self.store_quotes(matched)
                detail = f"demo: {stored} quotes ({unmatched} unmatched)"
                usage = None
            else:
                from .sources.odds_api import OddsApiSource

                source = OddsApiSource()
                if not source.enabled:
                    raise SourceError(
                        "no ODDS_API_KEY set — falling back to ESPN's single consensus line"
                    )
                quotes = source.fetch_odds(force=force)
                matched, unmatched = self.match_quotes(quotes)
                stored = self.store_quotes(matched)
                usage = source.usage()
                detail = f"{stored} quotes from {len({q['book'] for q in matched})} books"
                if unmatched:
                    detail += f" ({unmatched} unmatched)"
            # Games we have never built a consensus for get the full replay so
            # their movement history is populated from the quotes we just stored.
            fresh = [r["game_id"] for r in db.query(
                "SELECT DISTINCT o.game_id FROM odds_snapshots o WHERE NOT EXISTS "
                "(SELECT 1 FROM consensus c WHERE c.game_id = o.game_id)"
            )]
            rebuilt = self.rebuild_consensus(fresh, full_history=True) if fresh else 0
            rebuilt += self.rebuild_consensus()
            result.record("odds", True, detail, stored=stored, consensus=rebuilt, usage=usage)
            db.log_fetch("odds", True, detail, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001
            # Still rebuild consensus: the ESPN fallback line may have updated.
            try:
                self.rebuild_consensus()
            except Exception:  # noqa: BLE001
                pass
            result.record("odds", False, str(exc))
            db.log_fetch("odds", False, str(exc), int((time.monotonic() - start) * 1000))

    def refresh_prediction_markets(self, result: RefreshResult) -> None:
        """Poll Polymarket and store it as its own venue.

        Deliberately a separate stage from ``odds``: it is a different kind of
        venue, it must never reach the sportsbook consensus, and it should be
        able to fail without touching the sharp lines the rest of the app runs
        on.
        """
        start = time.monotonic()
        try:
            if not self.config.prediction_markets_enabled:
                result.record("prediction_markets", True, "disabled by configuration")
                return
            if self.demo:
                quotes = _demo_prediction_market(self)
            else:
                from .sources.polymarket import PolymarketSource

                quotes = PolymarketSource().fetch(with_depth=True)
            if not quotes:
                raise SourceError("no NFL markets returned")

            stamp = now_iso()
            rows = [q.to_quote_row(stamp) for q in quotes]
            matched, unmatched = self.match_quotes(rows)
            # Orientation: our schedule decides home and away, so a market that
            # named the teams the other way round is flipped rather than dropped.
            oriented = self._orient_prediction_quotes(matched)
            stored = self.store_quotes(oriented)

            depth = {}
            for quote, row in zip(quotes, rows, strict=False):
                gid = next((m["game_id"] for m in oriented
                            if m.get("provider_event_id") == row.get("provider_event_id")), None)
                if gid and quote.depth:
                    depth[gid] = quote.depth
            db.set_meta("prediction_market_depth", depth)

            detail = f"{stored} quotes from polymarket ({unmatched} unmatched)"
            result.record("prediction_markets", True, detail, stored=stored)
            db.log_fetch("prediction_markets", True, detail,
                         int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001
            result.record("prediction_markets", False, str(exc))
            db.log_fetch("prediction_markets", False, str(exc),
                         int((time.monotonic() - start) * 1000))

    def _orient_prediction_quotes(self, quotes: list[dict]) -> list[dict]:
        """Flip any quote whose home/away are reversed relative to our schedule."""
        if not quotes:
            return quotes
        lookup = {
            r["game_id"]: r for r in db.query("SELECT game_id, home, away FROM games")
        }
        out: list[dict] = []
        for q in quotes:
            game = lookup.get(q.get("game_id"))
            if not game:
                continue
            if q.get("home") == game["away"] and q.get("away") == game["home"]:
                q = {
                    **q,
                    "home": game["home"], "away": game["away"],
                    "home_price": q.get("away_price"), "away_price": q.get("home_price"),
                    "home_point": q.get("away_point"), "away_point": q.get("home_point"),
                }
            elif q.get("home") != game["home"]:
                continue
            out.append(q)
        return out

    def refresh_news(self, result: RefreshResult) -> None:
        start = time.monotonic()
        try:
            if self.demo:
                from .sources.demo import generate_news

                items = generate_news(self.season())
                injuries: list[dict] = []
            else:
                from .sources.espn import EspnSource
                from .sources.news_rss import NewsSource

                espn = EspnSource()
                items = NewsSource().fetch() + espn.news()
                injuries = espn.injuries()

            tagged = tag_items(items)
            stamp = now_iso()
            db.executemany(
                "INSERT INTO news(id, source, published_at, fetched_at, title, url, summary,"
                " teams, players, category, impact, line_impact) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET impact=excluded.impact, "
                "category=excluded.category, teams=excluded.teams, "
                "line_impact=excluded.line_impact",
                [
                    [
                        item["id"], item.get("source", ""), item.get("published_at"), stamp,
                        item.get("title", ""), item.get("url", ""), item.get("summary", ""),
                        json.dumps(item.get("teams") or []), json.dumps([]),
                        item.get("category", "general"), float(item.get("impact") or 0.0),
                        item.get("line_impact"),
                    ]
                    for item in tagged
                ],
            )
            if injuries:
                db.executemany(
                    "INSERT OR IGNORE INTO injuries(team, player, position, status, detail, updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    [
                        [i["team"], i["player"], i.get("position"), i.get("status"),
                         i.get("detail"), i.get("updated_at") or stamp]
                        for i in injuries
                    ],
                )
            detail = f"{len(tagged)} items, {len(injuries)} injury rows"
            result.record("news", True, detail, count=len(tagged))
            db.log_fetch("news", True, detail, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001
            result.record("news", False, str(exc))
            db.log_fetch("news", False, str(exc), int((time.monotonic() - start) * 1000))

    def refresh_stats(self, result: RefreshResult) -> None:
        """Season-to-date EPA. Optional: everything still works without it."""
        start = time.monotonic()
        try:
            if self.demo:
                result.record("stats", True, "demo mode: EPA not simulated", count=0)
                return
            from .sources.nflverse import NflverseSource

            season = self.season()
            nfl = NflverseSource()
            epa = nfl.team_epa(season)
            payload = epa.to_dict("records") if epa is not None and len(epa) else []
            db.set_meta("team_epa", {"season": season, "captured_at": now_iso(), "rows": payload})

            # The per-game detail the market-blind features roll up. The current
            # and prior season are enough: rolling state only looks back that far.
            stored = self.store_team_game_stats(nfl, [season - 1, season])
            detail = f"EPA for {len(payload)} teams, {stored} game-team rows"
            result.record("stats", True, detail, count=len(payload))
            db.log_fetch("stats", True, detail, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001
            result.record("stats", False, str(exc))
            db.log_fetch("stats", False, str(exc), int((time.monotonic() - start) * 1000))

    def store_team_game_stats(self, source, seasons: list[int]) -> int:
        """Cache per-game team detail for the given seasons."""
        stamp = now_iso()
        rows: list[list] = []
        for season in seasons:
            try:
                frame = source.game_team_stats(season)
            except Exception:  # noqa: BLE001 - a missing season must not fail the stage
                continue
            if frame is None or len(frame) == 0:
                continue
            for record in frame.to_dict("records"):
                clean = {
                    k: (None if v is None or v != v else v)
                    for k, v in record.items()
                    if not isinstance(v, (list, dict))
                }
                rows.append([
                    str(record.get("game_id")), record.get("team"),
                    int(record.get("season") or season), int(record.get("week") or 0),
                    record.get("opponent"), json.dumps(clean, default=str), stamp,
                ])
        db.executemany(
            "INSERT OR REPLACE INTO team_game_stats"
            "(game_id, team, season, week, opponent, payload, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def load_team_game_stats(self, season: int) -> dict[str, dict]:
        """game_id -> {team: stats}, for the feature builder."""
        out: dict[str, dict] = {}
        for row in db.query(
            "SELECT game_id, team, payload FROM team_game_stats WHERE season <= ?", (season,)
        ):
            try:
                out.setdefault(row["game_id"], {})[row["team"]] = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------------- recompute
    def load_games(self, season: int | None = None) -> list[dict]:
        season = season or self.season()
        return db.query(
            "SELECT * FROM games WHERE season = ? ORDER BY week, kickoff", (season,)
        )

    def recompute(self, result: RefreshResult | None = None) -> dict:
        """Ratings → predictions → simulation → picks. The analytical core."""
        start = time.monotonic()
        season = self.season()
        week = self.current_week(season)
        games = self.load_games(season)
        if not games:
            if result:
                result.record("recompute", False, "no games in database")
            return {}

        consensus = self.latest_consensus(season)
        for g in games:
            row = consensus.get(g["game_id"])
            if row:
                g["spread_home"] = row["spread_home"]
                g["market_total"] = row["total_points"]

        # ---- ratings: Elo walks every completed game we have, across seasons,
        # so a Week 1 rating reflects last year rather than a flat prior.
        completed = db.query(
            "SELECT * FROM games WHERE status = 'final' AND season <= ? "
            "ORDER BY season, week, kickoff",
            (season,),
        )
        elo = run_elo(completed)
        epa_meta = db.get_meta("team_epa", {}) or {}
        efficiencies = {}
        if epa_meta.get("season") == season and epa_meta.get("rows"):
            efficiencies = from_epa_frame(pd.DataFrame(epa_meta["rows"]))
        power = build_power_ratings(
            elo.as_points(), efficiencies, week=week, elo_raw=elo.snapshot()
        )

        stamp = now_iso()
        db.executemany(
            "INSERT OR REPLACE INTO team_ratings"
            "(team, season, captured_at, elo, off_epa, def_epa, pace, power, off_rating, def_rating) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                [t.team, season, stamp, t.elo, t.off_epa, t.def_epa, None,
                 t.power, t.off_rating, t.def_rating]
                for t in power.teams.values()
            ],
        )

        # ---- predictions
        # Features are built over the whole history so rolling form and Elo
        # cross the season boundary, then narrowed to the season on display.
        history = db.query(
            "SELECT * FROM games WHERE season <= ? ORDER BY season, week, kickoff", (season,)
        )
        consensus_all = self.latest_consensus()
        for g in history:
            row = consensus_all.get(g["game_id"])
            if row:
                g["spread_home"] = row["spread_home"]
                g["market_total"] = row["total_points"]
        full_frame = build_features(
            history, team_game_stats=self.load_team_game_stats(season)
        )
        frame = full_frame[full_frame["season"] == season] if not full_frame.empty else full_frame
        predictions = self.predictor.predict_frame(frame, power)
        by_game = {p.game_id: p for p in predictions}
        upcoming = {g["game_id"] for g in games if g["status"] != "final"}
        db.executemany(
            "INSERT OR REPLACE INTO predictions"
            "(game_id, captured_at, model_version, margin_home, total_points, home_win_prob,"
            " market_spread, market_total, spread_edge, total_edge, components) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                [
                    p.game_id, stamp, self.predictor.version, p.model_margin, p.model_total,
                    p.home_win_prob, p.market_spread, p.market_total, p.spread_edge,
                    p.total_edge, json.dumps(p.components),
                ]
                for p in predictions if p.game_id in upcoming
            ],
        )

        # ---- season simulation
        margins = {gid: p.fair_margin for gid, p in by_game.items()}
        sim = simulate_season(season, games, power, game_margins=margins, n_sims=20000)
        win_totals = db.get_meta("season_win_totals", {}) or {}
        db.executemany(
            "INSERT OR REPLACE INTO season_projections"
            "(team, season, captured_at, wins_actual, losses_actual, ties_actual, exp_wins,"
            " wins_p10, wins_p90, playoff_prob, division_prob, bye_prob, sb_prob,"
            " win_total_line, over_prob, distribution) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                [
                    t.team, season, stamp, t.wins_actual, t.losses_actual, t.ties_actual,
                    t.exp_wins, t.wins_p10, t.wins_p90, t.playoff_prob, t.division_prob,
                    t.bye_prob, t.sb_prob,
                    win_totals.get(t.team),
                    _over_prob(t, win_totals.get(t.team)),
                    json.dumps({str(k): v for k, v in t.distribution.items()}),
                ]
                for t in sim.teams.values()
            ],
        )

        # ---- picks
        self._store_picks(season, week, games, by_game, consensus)

        # ---- grading
        from .backtest.grade import grade_completed_games

        graded = grade_completed_games(season)

        db.set_meta("last_recompute", stamp)
        detail = (
            f"{len(predictions)} predictions, {len(sim.teams)} team projections, "
            f"{graded} newly graded"
        )
        if result:
            result.record("recompute", True, detail,
                          duration_ms=int((time.monotonic() - start) * 1000))
        return {"season": season, "week": week, "predictions": len(predictions), "graded": graded}

    def _store_picks(self, season: int, week: int, games: list[dict],
                     by_game: dict, consensus: dict) -> None:
        stamp = now_iso()
        upcoming = [g for g in games if g["status"] != "final"]

        # ---- betting edges for the current week
        edges: list[dict] = []
        for g in upcoming:
            if int(g["week"]) != week:
                continue
            prediction = by_game.get(g["game_id"])
            if not prediction:
                continue
            row = consensus.get(g["game_id"])
            if not row:
                continue
            found = find_edges(
                prediction, _consensus_from_row(row, g["game_id"]), g,
                residual_sd=self.predictor.residual_sd,
            )
            edges.extend(e.to_dict() for e in found)
        edges.sort(key=lambda e: e["expected_value"], reverse=True)

        # ---- pick'em (both modes) and survivor
        entries = []
        for g in upcoming:
            if int(g["week"]) != week:
                continue
            prediction = by_game.get(g["game_id"])
            if not prediction:
                continue
            row = consensus.get(g["game_id"])
            entries.append({
                "game_id": g["game_id"], "home": g["home"], "away": g["away"],
                "home_win_prob": prediction.home_win_prob,
                "market_home_prob": (row or {}).get("home_win_prob"),
                "kickoff": g["kickoff"],
            })

        boards = {
            mode: build_pickem(season, week, entries, mode=mode).to_dict()
            for mode in ("ev", "leverage")
        }

        by_week: dict[int, list[dict]] = {}
        for g in upcoming:
            prediction = by_game.get(g["game_id"])
            if not prediction:
                continue
            by_week.setdefault(int(g["week"]), []).append({
                "game_id": g["game_id"], "home": g["home"], "away": g["away"],
                "home_win_prob": prediction.home_win_prob, "kickoff": g["kickoff"],
            })
        used = db.get_meta("survivor_used_teams", []) or []
        survivor = plan_survivor(season, week, by_week, used_teams=used, horizon=6).to_dict()

        cross = self._cross_market_edges(season, week, upcoming, consensus)

        for contest, payload in (
            ("ats", {"edges": edges}),
            ("crossmarket", {"edges": cross}),
            ("pickem", boards),
            ("survivor", survivor),
        ):
            db.execute(
                "INSERT OR REPLACE INTO pick_history(contest, season, week, captured_at, payload) "
                "VALUES(?,?,?,?,?)",
                (contest, season, week, stamp, json.dumps(payload)),
            )

    def backfill_predictions(self, season: int | None = None, *, overwrite: bool = False) -> int:
        """Store what the model would have said about games already played.

        Legitimate because features are leak-free by construction: a game's row
        is built only from information available before its kickoff.  The
        caveat that matters is training scope — if the model was *trained* on
        these seasons, the resulting grades are in-sample and will flatter it.
        Such rows are marked with a ``:backfill`` suffix on the model version so
        the performance view can say so out loud, and the walk-forward numbers in
        the training report remain the honest measure.
        """
        season = season or self.season()
        history = db.query(
            "SELECT * FROM games WHERE season <= ? ORDER BY season, week, kickoff", (season,)
        )
        if not history:
            return 0
        consensus = self.latest_consensus()
        for g in history:
            row = consensus.get(g["game_id"])
            if row:
                g["spread_home"] = row["spread_home"]
                g["market_total"] = row["total_points"]

        existing = {
            r["game_id"] for r in db.query("SELECT DISTINCT game_id FROM predictions")
        } if not overwrite else set()
        final_ids = {g["game_id"] for g in history if g["status"] == "final"}
        wanted = final_ids - existing
        if not wanted:
            return 0

        frame = build_features(
            history, team_game_stats=self.load_team_game_stats(season)
        )
        frame = frame[frame["game_id"].isin(wanted)]
        if frame.empty:
            return 0

        # Ratings as of now are fine for the power component here; the ML and
        # market components are the ones that carry the per-game timing.
        elo = run_elo([g for g in history if g["status"] == "final"])
        power = build_power_ratings(elo.as_points(), week=18, elo_raw=elo.snapshot())
        predictions = self.predictor.predict_frame(frame, power)
        version = f"{self.predictor.version}:backfill"
        stamp = now_iso()
        db.executemany(
            "INSERT OR REPLACE INTO predictions"
            "(game_id, captured_at, model_version, margin_home, total_points, home_win_prob,"
            " market_spread, market_total, spread_edge, total_edge, components) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                [p.game_id, stamp, version, p.model_margin, p.model_total, p.home_win_prob,
                 p.market_spread, p.market_total, p.spread_edge, p.total_edge,
                 json.dumps(p.components)]
                for p in predictions
            ],
        )
        return len(predictions)

    def _cross_market_edges(self, season: int, week: int, upcoming: list[dict],
                            consensus: dict) -> list[dict]:
        """Where a prediction market disagrees with the sportsbook consensus."""
        from .picks.crossmarket import find_cross_market_edges
        from .sources.polymarket import VENUE

        games = [g for g in upcoming if int(g["week"]) == week]
        if not games:
            return []
        ids = [g["game_id"] for g in games]
        placeholders = ",".join("?" for _ in ids)
        rows = db.query(
            "SELECT o.game_id, o.home_price, o.away_price FROM odds_snapshots o "
            "JOIN (SELECT game_id, MAX(captured_at) AS m FROM odds_snapshots "
            f"      WHERE book = ? AND market = 'moneyline' AND game_id IN ({placeholders}) "
            "       GROUP BY game_id) x "
            "ON x.game_id = o.game_id AND x.m = o.captured_at "
            "WHERE o.book = ? AND o.market = 'moneyline'",
            [VENUE, *ids, VENUE],
        )
        probs: dict[str, float] = {}
        for row in rows:
            pair = devig(
                american_to_prob(row["home_price"]), american_to_prob(row["away_price"])
            )
            if pair:
                probs[row["game_id"]] = pair[0]
        if not probs:
            return []

        consensus_objects = {
            gid: _consensus_from_row(consensus[gid], gid)
            for gid in probs if gid in consensus
        }
        depths = db.get_meta("prediction_market_depth", {}) or {}
        found = find_cross_market_edges(
            games, consensus_objects, probs, depths=depths, venue=VENUE
        )
        return [e.to_dict() for e in found]

    # ------------------------------------------------------------ full refresh
    def refresh(self, stages: list[str] | None = None, *, force_odds: bool = False) -> RefreshResult:
        stages = stages or [
            "schedule", "odds", "prediction_markets", "news", "stats", "recompute",
        ]
        result = RefreshResult()
        if "schedule" in stages:
            self.refresh_schedule(result)
        if "odds" in stages:
            self.refresh_odds(result, force=force_odds)
        if "prediction_markets" in stages:
            self.refresh_prediction_markets(result)
        if "news" in stages:
            self.refresh_news(result)
        if "stats" in stages:
            self.refresh_stats(result)
        if "recompute" in stages:
            try:
                self.recompute(result)
            except Exception as exc:  # noqa: BLE001
                result.record("recompute", False, str(exc))
        result.finished_at = now_iso()
        db.set_meta("last_refresh", result.to_dict())
        return result

    def bootstrap(self) -> RefreshResult:
        """First run: make sure the app has something to show."""
        row = db.query_one("SELECT COUNT(*) AS n FROM games")
        if row and row["n"]:
            return self.refresh(["odds", "news", "recompute"])
        return self.refresh()


def _demo_prediction_market(pipeline) -> list:
    """Synthetic prediction-market prices for demo mode.

    Deliberately biased and noisy relative to the demo sportsbooks, so the
    cross-market panel has something to show and the depth filter is exercised.
    """
    import random

    from .sources.polymarket import PolymarketQuote

    season = pipeline.season()
    week = pipeline.current_week(season)
    games = db.query(
        "SELECT game_id, home, away, kickoff FROM games "
        "WHERE season = ? AND week = ? AND status != 'final'",
        (season, week),
    )
    rng = random.Random(season * 1000 + week)
    quotes = []
    for game in games:
        row = db.query_one(
            "SELECT home_win_prob FROM consensus WHERE game_id = ? "
            "ORDER BY captured_at DESC LIMIT 1", (game["game_id"],)
        )
        fair = (row or {}).get("home_win_prob")
        if fair is None:
            continue
        # A thin venue that lags: mostly close, occasionally well off.
        drift = rng.gauss(0, 0.03) + (rng.choice([-0.09, 0.09]) if rng.random() < 0.25 else 0)
        price = min(0.97, max(0.03, float(fair) + drift))
        quote = PolymarketQuote(
            home=game["home"], away=game["away"],
            home_price=price, away_price=1 - price,
            kickoff=game["kickoff"], market_id=f"demo-pm-{game['game_id']}",
            volume=rng.uniform(2000, 60000),
        )
        quote.depth = {
            game["home"]: rng.uniform(100, 4000),
            game["away"]: rng.uniform(100, 4000),
        }
        quotes.append(quote)
    return quotes


def _over_prob(team_season, line: float | None) -> float | None:
    """P(team finishes over its market season win total)."""
    if line is None:
        return None
    over = sum(p for wins, p in team_season.distribution.items() if wins > line)
    return round(over, 4)


def _consensus_from_row(row: dict, game_id: str):
    """Rehydrate a Consensus object from a stored row, including best prices."""
    from . import db as _db
    from .market.consensus import Consensus, latest_per_book, sportsbook_quotes

    consensus = Consensus(
        game_id=game_id,
        captured_at=row["captured_at"],
        spread_home=row["spread_home"],
        spread_price_home=row["spread_price_home"],
        spread_price_away=row["spread_price_away"],
        total_points=row["total_points"],
        total_price_over=row["total_price_over"],
        total_price_under=row["total_price_under"],
        ml_home=row["ml_home"],
        ml_away=row["ml_away"],
        home_win_prob=row["home_win_prob"],
        n_books=row["n_books"] or 0,
        books=json.loads(row["books"] or "[]"),
    )
    quotes = _db.query(
        "SELECT book, market, captured_at, home_point, away_point, home_price, away_price "
        "FROM odds_snapshots WHERE game_id = ? ORDER BY captured_at",
        (game_id,),
    )
    # Best-available pricing must stay inside the sportsbook universe. A
    # prediction-market price is the thing the cross-market panel bets *into*;
    # quoting a model edge against it would merge two different claims and
    # attribute a sportsbook recommendation to a venue that never offered it.
    quotes = sportsbook_quotes(quotes)
    newest = latest_per_book(quotes)
    from .market.consensus import _best, _best_price

    spreads = [q for (b, m), q in newest.items() if m == "spread"]
    totals = [q for (b, m), q in newest.items() if m == "total"]
    moneylines = [q for (b, m), q in newest.items() if m == "moneyline"]
    if spreads:
        consensus.best_home_spread = _best(spreads, "home_point", maximum=True)
        consensus.best_away_spread = _best(spreads, "away_point", maximum=True)
    if totals:
        consensus.best_over = _best(totals, "home_point", maximum=False)
        consensus.best_under = _best(totals, "away_point", maximum=True)
    if moneylines:
        consensus.best_ml_home = _best_price(moneylines, "home_price")
        consensus.best_ml_away = _best_price(moneylines, "away_price")
    return consensus


_DEMO_CACHE: dict[tuple[int, int], dict] = {}


def _demo_season(season: int, through_week: int) -> dict:
    """Cache synthetic seasons; regenerating them on every poll is wasteful."""
    key = (season, through_week)
    if key not in _DEMO_CACHE:
        from .sources.demo import generate_season

        if len(_DEMO_CACHE) > DEMO_HISTORY_SEASONS + 2:
            _DEMO_CACHE.clear()
        _DEMO_CACHE[key] = generate_season(season, through_week=through_week)
    return _DEMO_CACHE[key]
