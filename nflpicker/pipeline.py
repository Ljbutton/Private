"""Refresh orchestration: fetch, store, recompute.

Each refresh stage is independent and failure-isolated.  A dead RSS feed must
not stop odds from updating, and an exhausted Odds API quota must not stop
scores from coming in.  Anything that fails is recorded in ``fetch_log`` and
surfaced in the UI as a source-health row rather than thrown away.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
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
from .sim.season import anchor_to_market, simulate_season
from .sources.base import SourceError
from .util import (
    current_season,
    estimate_week,
    now_iso,
    seconds_since,
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



log = logging.getLogger("nflpicker.pipeline")

# The most of the standing injury report one refresh may retire. A smell test
# rather than a tuned number: real recoveries trickle in a few at a time across
# a week, so one response clearing more than half the league's listed players
# is describing a fault upstream, not a Tuesday.
CLEAR_LIMIT = 0.5
# ...but only once there is enough of a report for a proportion to mean
# anything. With three players listed, clearing two is 67% and entirely
# ordinary; without this floor the guard fires hardest on exactly the small,
# early-season reports where every clearing is legitimate.
CLEAR_LIMIT_FLOOR = 10

# The season training starts from, matching the CLI's default. Earlier seasons
# exist but predate the play-by-play detail most features are built on.
TRAIN_SINCE_SEASON = 2002


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


def _ago(seconds: float) -> str:
    """A duration a person reads at a glance: "4m", "2h", "3d"."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    if seconds < 172800:
        return f"{round(seconds / 3600)}h"
    return f"{round(seconds / 86400)}d"


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
                games = _demo_live_games(payload["games"], week)
                count += self.upsert_games(games)
                live = self.store_live_state(games)
                if live:
                    self.refresh_live_probabilities()
                detail = (
                    f"demo seasons {season - DEMO_HISTORY_SEASONS}-{season}, {count} games"
                )
                if live:
                    detail += f", {live} live"
            else:
                from .sources.espn import EspnSource

                season = self.season()
                espn = EspnSource()
                games = espn.season_schedule(season)
                if not games:
                    raise SourceError("ESPN returned no games")
                count = self.upsert_games(games)
                live = self.store_live_state(games)
                if live:
                    self.refresh_live_probabilities()
                detail = f"{count} games from ESPN"
                if live:
                    detail += f", {live} live"
                self._store_espn_fallback_odds(games)
            result.record("schedule", True, detail, count=count)
            db.log_fetch("schedule", True, detail, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001
            result.record("schedule", False, str(exc))
            db.log_fetch("schedule", False, str(exc), int((time.monotonic() - start) * 1000))

    def store_live_state(self, games: list[dict]) -> int:
        """Persist in-game state, and clear it once a game is final."""
        stamp = now_iso()
        rows = []
        finished = []
        for game in games:
            live = game.get("live")
            if game.get("status") == "final" or not live:
                finished.append(game["game_id"])
                continue
            rows.append([
                game["game_id"], stamp, live.get("period"), live.get("clock"),
                live.get("seconds_left"), live.get("possession"), live.get("down"),
                live.get("distance"), live.get("yard_line"), int(bool(live.get("red_zone"))),
                live.get("home_timeouts"), live.get("away_timeouts"),
                live.get("last_play"), live.get("detail"),
                game.get("home_score"), game.get("away_score"), None,
            ])
        db.executemany(
            "INSERT OR REPLACE INTO live_state(game_id, updated_at, period, clock, "
            "seconds_left, possession, down, distance, yard_line, red_zone, "
            "home_timeouts, away_timeouts, last_play, detail, home_score, away_score, "
            "win_prob_home) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        # A finished game is no longer live; leaving the row behind would show a
        # stale third-quarter clock next to a final score.
        if finished:
            placeholders = ",".join("?" for _ in finished)
            db.execute(
                f"DELETE FROM live_state WHERE game_id IN ({placeholders})", finished
            )
        return len(rows)

    def refresh_live_probabilities(self) -> int:
        """Recompute live win probability for every game in progress.

        Kept separate from the scoreboard fetch so it can run on the cheap
        cadence the scoreboard already uses, without waiting for a full
        recompute — the pregame projection it leans on changes far more slowly
        than the scoreboard does.
        """
        from .live import LiveState, win_probability

        rows = db.query(
            "SELECT l.*, g.home, g.away FROM live_state l "
            "JOIN games g ON g.game_id = l.game_id"
        )
        updates = []
        for row in rows:
            prediction = db.query_one(
                "SELECT margin_home FROM predictions WHERE game_id = ? "
                "ORDER BY captured_at DESC LIMIT 1",
                (row["game_id"],),
            )
            pregame = float((prediction or {}).get("margin_home") or 0.0)
            state = LiveState(
                period=row["period"], seconds_left=row["seconds_left"],
                possession=row["possession"], down=row["down"],
                distance=row["distance"], yard_line=row["yard_line"],
                red_zone=bool(row["red_zone"]),
                home_score=int(row["home_score"] or 0),
                away_score=int(row["away_score"] or 0),
            )
            updates.append([
                win_probability(state, pregame, row["home"]), row["game_id"]
            ])
        db.executemany(
            "UPDATE live_state SET win_prob_home = ? WHERE game_id = ?", updates
        )
        return len(updates)

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
            stamp = now_iso()
            rows: list[dict] = []
            reached: list[str] = []
            failed: list[str] = []

            if self.demo:
                rows = [q.to_quote_row(stamp) for q in _demo_prediction_market(self)]
                reached = sorted({r["book"] for r in rows})
            else:
                from .sources.kalshi import KalshiSource
                from .sources.polymarket import PolymarketSource

                # Each venue is fetched independently: one being down or having
                # renamed a series must not cost us the other.
                for name, fetch in (
                    ("polymarket", lambda: PolymarketSource().fetch(with_depth=False)),
                    ("kalshi", lambda: KalshiSource().fetch()),
                ):
                    try:
                        venue_quotes = fetch()
                    except Exception as exc:  # noqa: BLE001
                        failed.append(f"{name}: {exc}")
                        continue
                    if venue_quotes:
                        rows.extend(q.to_quote_row(stamp) for q in venue_quotes)
                        reached.append(name)

            if not rows:
                raise SourceError("; ".join(failed) or "no NFL markets returned")

            matched, unmatched = self.match_quotes(rows)
            # Orientation: our schedule decides home and away, so a market that
            # named the teams the other way round is flipped rather than dropped.
            oriented = self._orient_prediction_quotes(matched)
            stored = self.store_quotes(oriented)

            detail = f"{stored} quotes from {', '.join(reached) or 'no venue'}"
            if unmatched:
                detail += f" ({unmatched} unmatched)"
            if failed:
                detail += f" — unavailable: {'; '.join(failed)[:120]}"
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

    def refresh_weather(self, result: RefreshResult) -> None:
        """Forecast at kickoff for upcoming games.

        The model already has temperature and wind as features because the
        historical record carries them, but nothing was filling them for games
        that had not been played — so two trained-on columns arrived empty at
        inference every time. Wind is the one that matters: above roughly
        15 mph it is the largest weather effect on scoring.
        """
        start = time.monotonic()
        try:
            games = db.query(
                "SELECT game_id, home, kickoff FROM games "
                "WHERE status = 'scheduled' AND kickoff IS NOT NULL AND kickoff > ? "
                "ORDER BY kickoff LIMIT 48",
                (now_iso(),),
            )
            if not games:
                result.record("weather", True, "no upcoming games")
                return

            if self.demo:
                from .sources.demo import generate_weather

                forecasts = generate_weather(games)
            else:
                from .sources.weather import WeatherSource

                source = WeatherSource()
                forecasts = {}
                for game in games:
                    forecast = source.for_game(game["home"], game["kickoff"])
                    if forecast:
                        forecasts[game["game_id"]] = forecast

            stamp = now_iso()
            db.executemany(
                "INSERT OR REPLACE INTO game_weather"
                "(game_id, updated_at, roof, indoor, temp_f, wind_mph, precip_pct) "
                "VALUES(?,?,?,?,?,?,?)",
                [
                    [gid, stamp, f.get("roof"), int(bool(f.get("indoor"))),
                     f.get("temp_f"), f.get("wind_mph"), f.get("precip_pct")]
                    for gid, f in forecasts.items()
                ],
            )
            windy = sum(1 for f in forecasts.values() if (f.get("wind_mph") or 0) >= 15)
            detail = f"{len(forecasts)} forecasts"
            if windy:
                detail += f", {windy} windy"
            result.record("weather", True, detail, count=len(forecasts))
            db.log_fetch("weather", True, detail, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001
            result.record("weather", False, str(exc))
            db.log_fetch("weather", False, str(exc), int((time.monotonic() - start) * 1000))

    def load_weather(self) -> dict[str, dict]:
        return {r["game_id"]: r for r in db.query("SELECT * FROM game_weather")}

    def refresh_news(self, result: RefreshResult) -> None:
        start = time.monotonic()
        try:
            if self.demo:
                from .sources.demo import generate_injuries, generate_news

                items = generate_news(self.season())
                injuries = generate_injuries(self.season())
                covered = None
            else:
                from .sources.espn import EspnSource
                from .sources.news_rss import NewsSource

                espn = EspnSource()
                items = NewsSource().fetch() + espn.news()
                injuries = espn.injuries()
                covered = getattr(espn, "covered_teams", None)

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
            # Named before the branch because the summary line below reports
            # it whether or not the feed returned anything to compare against.
            cleared: list[tuple[str, str]] = []
            if injuries:
                # Only write when a player's status has actually changed. The
                # feed is polled every fifteen minutes; storing every player
                # every time would add tens of thousands of identical rows a
                # day and make "current status" a scan rather than a lookup.
                from .availability import is_notable_injury

                current = {
                    (r["team"], r["player"]): r
                    for r in db.query(
                        # Keyed on the last row written rather than the latest
                        # date it carries -- see the same query in the API for
                        # why those are not the same row.
                        "SELECT i.team, i.player, i.status, i.injury, i.return_date,"
                        " i.first_seen FROM injuries i "
                        "JOIN (SELECT team, player, MAX(id) AS m FROM injuries "
                        "      GROUP BY team, player) x ON x.team = i.team "
                        "AND x.player = i.player AND x.m = i.id"
                    )
                }
                # Status is no longer the only field worth a row. A return date
                # being announced, or a vague listing turning into "Right
                # Hamstring Strain", is exactly the update the report exists to
                # carry -- and while the status stayed "Questionable" through
                # both, neither would ever have been written.
                def moved(row: dict) -> bool:
                    was = current.get((row["team"], row["player"]))
                    if was is None:
                        return True
                    return any(was[key] != row.get(key)
                               for key in ("status", "injury", "return_date"))

                changed = [i for i in injuries if moved(i)]
                rows = []
                for i in changed:
                    was = current.get((i["team"], i["player"]))
                    # When this spell started. Carried forward while the player
                    # stays on the report, and restarted when someone who had
                    # cleared it is listed again -- otherwise a player hurt in
                    # September and again in December reads as hurt since
                    # September, which is the opposite of what the column says.
                    if was and was["first_seen"] and is_notable_injury(was["status"]):
                        first_seen = was["first_seen"]
                    else:
                        # A source that knows when the spell began is better
                        # than assuming it began the moment we first polled --
                        # otherwise every player looks newly hurt on the day
                        # the app is installed.
                        first_seen = (i.get("first_seen")
                                      or i.get("updated_at") or stamp)
                    rows.append([
                        i["team"], i["player"], i.get("position"), i.get("status"),
                        i.get("detail"), i.get("injury"), i.get("return_date"),
                        first_seen, i.get("updated_at") or stamp,
                    ])
                # A player who has cleared the report stops being mentioned by
                # the feed rather than being marked healthy. Since rows are
                # only written on a change, nothing ever contradicted the last
                # one -- so a hamstring from week 2 sat there reading "Out" in
                # week 12, and the report filled up with players who had been
                # fine for a month.
                #
                # Absence only means recovery inside a part of the list that
                # actually arrived, so clearing is scoped to the teams this
                # response covered. The feed is grouped by team, which is what
                # makes that scoping exact rather than a guess: a truncated
                # response drops whole teams, and a team we did not hear about
                # is one we learned nothing about. Clearing league-wide on a
                # partial response would mark healthy every player on every
                # team that happened to be missing.
                #
                # No covered set (the demo source, an older adapter) falls back
                # to the teams present in the rows. That is the weaker rule --
                # it cannot see a covered team whose players have all recovered
                # -- but it errs towards leaving a player listed, which is the
                # safe direction: a stale row is visible and correctable, a
                # wrongly cleared one looks like good news.
                scope = covered if covered else {i["team"] for i in injuries}
                listed = {(i["team"], i["player"]) for i in injuries}
                candidates = [
                    key for key, was in current.items()
                    if key not in listed and key[0] in scope
                    and is_notable_injury(was["status"])
                ]
                # A backstop for the case team scoping cannot catch: a covered
                # team whose group came back empty through a fault upstream.
                # Recoveries are gradual, so a response that retires most of
                # the standing report at once is far more likely to be broken
                # than true.
                standing = sum(1 for was in current.values()
                               if is_notable_injury(was["status"]))
                if (standing >= CLEAR_LIMIT_FLOOR
                        and len(candidates) > standing * CLEAR_LIMIT):
                    log.warning(
                        "injury feed would clear %d of %d listed players; "
                        "treating it as incomplete and clearing none",
                        len(candidates), standing)
                    cleared = []
                else:
                    cleared = candidates
                for team, player in cleared:
                    rows.append([team, player, None, "Active", None, None, None,
                                 None, stamp])

                db.executemany(
                    "INSERT OR REPLACE INTO injuries"
                    "(team, player, position, status, detail, injury, return_date,"
                    " first_seen, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                injuries = changed
            detail = f"{len(tagged)} items, {len(injuries)} injury rows"
            if cleared:
                detail += f", {len(cleared)} cleared"
            result.record("news", True, detail, count=len(tagged))
            db.log_fetch("news", True, detail, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001
            result.record("news", False, str(exc))
            db.log_fetch("news", False, str(exc), int((time.monotonic() - start) * 1000))

    def refresh_train(self, result: RefreshResult) -> None:
        """Refit the model as the season's results come in.

        Three conditions guard this, because retraining unattended is the one
        scheduled job that can make the app *worse*:

        * **Nothing live.** Training holds the refresh lock for minutes. Doing
          that while a game is in progress would stall the score poll exactly
          when it matters, so a live slate defers to the next check.
        * **Enough new evidence.** A week of results moves the weights; two or
          three games spend minutes of CPU to move them by nothing.
        * **No regression.** A fresh fit is trained into a temporary directory
          and promoted only if it is not materially worse than the one in
          place. An upstream schema change or a feature that quietly went empty
          would otherwise replace a good model with a broken one overnight,
          with the app reporting nothing but a new timestamp.
        """
        start = time.monotonic()
        try:
            if self.demo or not self.config.train_auto:
                result.record("train", True, "automatic training is off")
                return

            live = db.query_one(
                "SELECT COUNT(*) AS n FROM games WHERE status = 'in_progress'")
            if live and live["n"]:
                result.record("train", True, f"deferred: {live['n']} game(s) in progress")
                return

            done = db.query_one(
                "SELECT COUNT(*) AS n FROM games WHERE status = 'final'")
            completed = int((done or {}).get("n") or 0)
            trained_on = int(db.get_meta("train:n_games", 0) or 0)
            new_games = completed - trained_on
            # Two ways to earn a refit, because one was not enough. A week's
            # worth of results is the obvious one. The other is age: an NFL
            # week lands thirteen games on Sunday and then one on Monday and
            # one on Thursday, so a threshold that only a Sunday can clear
            # meant the model never saw a midweek result until the following
            # Sunday -- a Monday night injury or blowout sat unlearned for six
            # days. Once the fit is stale, one new result is enough.
            stale_after = self.config.train_max_age_hours * 3600.0
            age = seconds_since(db.get_meta("train:at")) if trained_on else None
            stale = age is not None and age >= stale_after
            enough = new_games >= self.config.train_min_new_games
            if trained_on and new_games <= 0:
                result.record("train", True, "no new results since the last fit")
                return
            if trained_on and not enough and not stale:
                hours = 0 if age is None else int(age // 3600)
                result.record(
                    "train", True,
                    f"{new_games} new result(s) since the last fit {hours}h ago; "
                    f"waiting for {self.config.train_min_new_games} "
                    f"or {self.config.train_max_age_hours}h")
                return

            detail = self._retrain(completed, new_games)
            result.record("train", True, detail)
            db.log_fetch("train", True, detail, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001 - a failed fit must not stop refreshing
            result.record("train", False, str(exc))
            db.log_fetch("train", False, str(exc), int((time.monotonic() - start) * 1000))

    def _retrain(self, completed: int, new_games: int) -> str:
        """Fit into a scratch directory, keep it only if it holds up."""
        import shutil
        import tempfile
        import warnings

        from .ml.dataset import from_nflverse
        from .ml.train import load_report, train

        incumbent = load_report()
        before = ((incumbent or {}).get("blind") or {}).get("margin_mae")

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            frame = from_nflverse(TRAIN_SINCE_SEASON)
            if frame.empty:
                return "no usable games"
            staging = Path(tempfile.mkdtemp(prefix="nflpicker-train-"))
            try:
                report = train(frame, model_dir=staging).to_dict()
                after = (report.get("blind") or {}).get("margin_mae")

                if before is not None and after is not None:
                    slippage = float(after) - float(before)
                    if slippage > self.config.train_max_regression:
                        return (
                            f"rejected: margin MAE {after:.3f} is {slippage:.3f} worse "
                            f"than the model in place ({before:.3f}); keeping it")

                model_dir = self.config.model_dir
                model_dir.mkdir(parents=True, exist_ok=True)
                for name in ("models.joblib", "training_report.json"):
                    shutil.copy2(staging / name, model_dir / name)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

        db.set_meta("train:n_games", completed)
        db.set_meta("train:at", now_iso())
        self.reload_model()
        mae = (report.get("blind") or {}).get("margin_mae")
        market = (report.get("blind") or {}).get("market_margin_mae")
        return (f"refit on {report.get('n_games')} games (+{new_games} new): "
                f"margin MAE {mae:.3f} vs market {market:.3f}")

    def refresh_stats(self, result: RefreshResult) -> None:
        """Season-to-date EPA. Optional: everything still works without it."""
        start = time.monotonic()
        try:
            if self.demo:
                from .sources.demo import (
                    generate_game_team_stats,
                    generate_team_efficiency,
                )

                season = self.season()
                rows = generate_team_efficiency(season)
                db.set_meta("team_epa", {"season": season, "captured_at": now_iso(),
                                         "rows": rows})

                # Per-game detail too, so the demo exercises the market-blind
                # feature path rather than leaving half the model untested.
                stamp = now_iso()
                detail = []
                for past in range(season - DEMO_HISTORY_SEASONS, season + 1):
                    games = db.query(
                        "SELECT * FROM games WHERE season = ? AND status = 'final'",
                        (past,),
                    )
                    detail.extend(generate_game_team_stats(games, past))
                db.executemany(
                    "INSERT OR REPLACE INTO team_game_stats"
                    "(game_id, team, season, week, opponent, payload, updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    [
                        [r["game_id"], r["team"], r["season"], r["week"],
                         r["opponent"], json.dumps(r), stamp]
                        for r in detail
                    ],
                )
                result.record("stats", True,
                              f"demo EPA for {len(rows)} teams, {len(detail)} game-team rows",
                              count=len(rows))
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

            # Depth charts name the actual backup. Falls back a season because
            # the release for a season that has not started yet is empty.
            depth = self.store_depth_charts(nfl, season)
            if not depth:
                depth = self.store_depth_charts(nfl, season - 1)

            detail = f"EPA for {len(payload)} teams, {stored} game-team rows"
            if depth:
                detail += f", {depth:,} depth-chart rows"
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

    def team_situational(self, season: int) -> dict[str, dict]:
        """Season-to-date situational rates per team, for display.

        Averaged over the season's games rather than exponentially weighted:
        this is a season summary a reader is comparing across teams, not the
        decayed form the model runs on.
        """
        rows = db.query(
            "SELECT team, payload FROM team_game_stats WHERE season = ?", (season,)
        )
        buckets: dict[str, dict[str, list[float]]] = {}
        keys = (
            "third_down_rate", "def_third_down_rate", "red_zone_td_rate",
            "explosive_rate", "def_explosive_rate", "sack_rate",
            "sack_rate_forced", "penalty_yards", "turnover_margin", "st_epa",
        )
        for row in rows:
            try:
                stats = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            team = buckets.setdefault(row["team"], {k: [] for k in keys})
            for key in keys:
                value = stats.get(key)
                if value is not None:
                    team[key].append(float(value))

        return {
            team: {
                key: (round(sum(vals) / len(vals), 4) if vals else None)
                for key, vals in stats.items()
            } | {"games": max((len(v) for v in stats.values()), default=0)}
            for team, stats in buckets.items()
        }

    def store_depth_charts(self, source, season: int) -> int:
        """Cache the latest weekly depth chart for the season."""
        from .availability import normalize_name

        try:
            frame = source.depth_charts(season)
        except Exception as exc:  # noqa: BLE001
            # Upstream has changed this layout before. Swallowing the error
            # returned zero rows and looked like "no data", which hid a schema
            # change for as long as nobody checked.
            db.log_fetch("depth_charts", False, f"{type(exc).__name__}: {exc}")
            return 0
        if frame is None or len(frame) == 0:
            return 0

        stamp = now_iso()
        # The 2025-onward release publishes dated snapshots rather than one row
        # per week, so a season arrives as ~520k rows that collapse onto ~3.7k
        # primary keys. Writing all of them costs 99% wasted inserts on every
        # refresh; de-duplicating here keeps identical semantics — the frame is
        # chronological and the last row for a key wins, which is exactly what
        # INSERT OR REPLACE was doing — at a fraction of the write volume.
        seen: dict[tuple, list] = {}
        for record in frame.to_dict("records"):
            player = normalize_name(record.get("full_name"))
            depth = record.get("depth")
            if not player or depth != depth or not record.get("position"):
                continue
            key = (
                int(record.get("season") or season), int(record.get("week") or 0),
                record["team"], str(record["position"]), int(depth),
            )
            seen[key] = [*key, player, stamp]
        rows = list(seen.values())
        db.executemany(
            "INSERT OR REPLACE INTO depth_chart"
            "(season, week, team, position, depth, player, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    def depth_by_position(self, season: int, position: str = "QB") -> dict[str, list[str]]:
        """Each team's most recent depth order at a position."""
        rows = db.query(
            "SELECT team, depth, player, week FROM depth_chart "
            "WHERE season = ? AND position = ? ORDER BY week, depth",
            (season, position),
        )
        latest_week: dict[str, int] = {}
        for row in rows:
            latest_week[row["team"]] = max(latest_week.get(row["team"], 0), row["week"])
        out: dict[str, list[str]] = {}
        for row in rows:
            if row["week"] != latest_week.get(row["team"]):
                continue
            # A player can appear at more than one depth slot; keep his best.
            players = out.setdefault(row["team"], [])
            if row["player"] not in players:
                players.append(row["player"])
        return out

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

    def quarterback_registry(self, season: int) -> tuple[dict[str, float], dict[str, list[str]]]:
        """Rolling value per quarterback, and each team's passers, by name key.

        Keyed by the normalised name rather than a player id because the injury
        feed and the play-by-play share no identifier.

        The estimator is deliberately the same one the feature builder uses for
        ``qb_value``: an exponentially weighted mean of per-game EPA, shrunk
        toward zero by dropback volume, regressed across a season boundary.
        It has to be. ``QB_VALUE_POINTS`` in the power rating was fitted
        against that quantity, and a coefficient measured on one estimator
        does not transfer to a different one — a plain arithmetic mean, which
        this used to take, weights a passer's rookie year the same as last
        Sunday and lands on a systematically different number, so the points
        the rating charged per unit were not the points that were measured.
        """
        from .availability import normalize_name
        from .ml.features import FORM_ALPHA, QB_PRIOR_DROPBACKS, SEASON_REGRESSION

        rows = db.query(
            "SELECT team, season, week, payload FROM team_game_stats "
            "WHERE season >= ? ORDER BY season, week",
            (season - 1,),
        )
        form: dict[str, float] = {}
        volume: dict[str, float] = {}
        depth: dict[str, list[str]] = {}
        current: int | None = None
        for row in rows:
            # Between seasons a passer's form is pulled back toward the league
            # mean, because rosters and schemes turn over enough that last
            # year's number is informative but not carried whole.
            year = int(row["season"])
            if current is not None and year != current:
                for key in form:
                    form[key] *= 1.0 - SEASON_REGRESSION
            current = year

            try:
                stats = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            key = normalize_name(stats.get("qb_name"))
            if not key:
                continue
            epa = stats.get("qb_epa")
            if epa is not None:
                observed = float(epa)
                previous = form.get(key)
                form[key] = observed if previous is None else (
                    FORM_ALPHA * observed + (1.0 - FORM_ALPHA) * previous
                )
            volume[key] = volume.get(key, 0.0) + float(stats.get("qb_dropbacks") or 0)
            seen = depth.setdefault(row["team"], [])
            if key in seen:
                seen.remove(key)
            seen.insert(0, key)   # most recent starter first

        values: dict[str, float] = {}
        for key, value in form.items():
            seen_dropbacks = volume.get(key, 0.0)
            weight = seen_dropbacks / (seen_dropbacks + QB_PRIOR_DROPBACKS)
            values[key] = value * weight
        return values, depth

    def qb_ids_by_name(self, season: int) -> dict[str, str]:
        """Normalised passer name -> the id the rating tracker knows him by.

        The injury report and the depth chart carry names; the feature builder
        keys quarterbacks by the play-by-play passer id. Without this bridge an
        announced replacement arrives as an unknown passer and is priced as a
        debut start even when he has thrown four hundred passes.

        Read from ``team_game_stats`` specifically, because that is the same
        source the rating tracker keys on — resolving a name against any other
        id space would produce a key that looks valid and matches nothing.
        """
        from .availability import normalize_name

        out: dict[str, str] = {}
        for row in db.query(
            "SELECT payload FROM team_game_stats WHERE season >= ? ORDER BY season, week",
            (season - 3,),
        ):
            try:
                stats = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            name = normalize_name(stats.get("qb_name"))
            qb_id = stats.get("qb_id")
            if name and qb_id:
                out[name] = str(qb_id)
        return out

    def expected_starters(self, season: int) -> dict[str, dict]:
        """Each team's expected starting quarterback for its next game."""
        from .starters import expected_starters

        depth = self.qb_depth(season)
        starters = expected_starters(depth, self.current_injuries())
        return {team: e.to_dict() for team, e in starters.items()}

    def current_injuries(self) -> dict[str, list[dict]]:
        """Latest injury report, keyed by team, in the shape the costers want."""
        from .availability import normalize_name

        rows = db.query(
            "SELECT i.team, i.player, i.position, i.status FROM injuries i "
            "JOIN (SELECT team, player, MAX(updated_at) AS m FROM injuries "
            "      GROUP BY team, player) x "
            "ON x.team = i.team AND x.player = i.player AND x.m = i.updated_at"
        )
        by_team: dict[str, list[dict]] = {}
        for row in rows:
            by_team.setdefault(row["team"], []).append({
                "player": normalize_name(row["player"]),
                "player_name": row["player"],
                "position": row["position"],
                "status": row["status"],
            })
        return by_team

    def qb_depth(self, season: int) -> dict[str, list[str]]:
        """Each team's passers in depth order: published chart, then who has
        actually been starting."""
        _, inferred = self.quarterback_registry(season)
        published = self.depth_by_position(season, "QB")
        return {**inferred, **{t: d for t, d in published.items() if d}}

    def availability_adjustments(
        self, season: int, qb_priced: set[str] | None = None
    ) -> dict[str, dict]:
        """Current injury report turned into points per team."""
        from .availability import build_adjustments, normalize_name

        rows = db.query(
            "SELECT i.team, i.player, i.position, i.status FROM injuries i "
            "JOIN (SELECT team, player, MAX(updated_at) AS m FROM injuries "
            "      GROUP BY team, player) x "
            "ON x.team = i.team AND x.player = i.player AND x.m = i.updated_at"
        )
        if not rows:
            return {}
        by_team: dict[str, list[dict]] = {}
        for row in rows:
            by_team.setdefault(row["team"], []).append({
                "player": normalize_name(row["player"]),
                "player_name": row["player"],
                "position": row["position"],
                "status": row["status"],
            })
        values, _ = self.quarterback_registry(season)
        depth = self.qb_depth(season)
        built = build_adjustments(
            by_team, qb_values=values, depth=depth, qb_priced=qb_priced
        )
        return {team: a.to_dict() for team, a in built.items()}

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
        power = self.power_from(completed, elo, season, week=week)

        stamp = now_iso()
        db.executemany(
            "INSERT OR REPLACE INTO team_ratings"
            "(team, season, captured_at, elo, off_epa, def_epa, pace, power,"
            " pythagorean, off_rating, def_rating) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                [t.team, season, stamp, t.elo, t.off_epa, t.def_epa, None,
                 t.power, t.pythagorean, t.off_rating, t.def_rating]
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
        weather = self.load_weather()
        for g in history:
            row = consensus_all.get(g["game_id"])
            if row:
                g["spread_home"] = row["spread_home"]
                g["market_total"] = row["total_points"]
            forecast = weather.get(g["game_id"])
            # Only fill what the historical record has not already supplied.
            if forecast and g.get("temp") is None:
                g["temp"] = forecast["temp_f"]
                g["wind"] = forecast["wind_mph"]
            if forecast and forecast["indoor"]:
                g["roof"] = forecast["roof"] or g.get("roof")
        # An announced starter change is the biggest single thing the schedule
        # feed cannot tell us before kickoff, so it is filled in here from the
        # injury report and the depth chart. Teams whose starter we replaced are
        # then excluded from the quarterback half of the availability offset:
        # the downgrade is inside the model now, and charging it twice would
        # double-count the most expensive absence in the sport.
        from .starters import apply_to_games
        from .starters import expected_starters as _expected

        starters = _expected(self.qb_depth(season), self.current_injuries())
        filled = apply_to_games(history, starters, self.qb_ids_by_name(season))
        qb_priced = {t for t, e in starters.items() if e.changed}
        db.set_meta(
            "expected_starters",
            {t: e.to_dict() for t, e in starters.items() if e.changed},
        )
        if qb_priced:
            db.log_fetch(
                "starters", True,
                f"{len(qb_priced)} announced change(s), {filled} sides filled",
            )

        full_frame = build_features(
            history, team_game_stats=self.load_team_game_stats(season)
        )
        frame = full_frame[full_frame["season"] == season] if not full_frame.empty else full_frame
        availability = self.availability_adjustments(season, qb_priced=qb_priced)
        db.set_meta("availability", availability)
        predictions = self.predictor.predict_frame(
            frame, power,
            adjustments={t: a["adjustment"] for t, a in availability.items()},
        )
        by_game = {p.game_id: p for p in predictions}
        upcoming = {g["game_id"] for g in games if g["status"] != "final"}

        # A finished game keeps whatever the model said while it was still
        # upcoming -- overwriting that with today's view would be grading the
        # model against a number it never had to commit to.
        #
        # But a database first filled in mid-season has no such row for the
        # weeks already played, and the board then shows the sportsbook's pick
        # in the model's column for every one of them. Those games are in this
        # same frame and already predicted, so they are stored here rather than
        # left blank -- tagged `:backfill`, the same marker the explicit
        # backfill uses, because a number produced after the fact is in-sample
        # and the performance page has to be able to say so.
        seen = {
            r["game_id"] for r in db.query(
                "SELECT DISTINCT p.game_id FROM predictions p JOIN games g"
                " ON g.game_id = p.game_id WHERE g.season = ?", (season,))
        }
        rows = []
        for p in predictions:
            live_row = p.game_id in upcoming
            if not live_row and p.game_id in seen:
                continue
            rows.append([
                p.game_id, stamp,
                self.predictor.version if live_row else f"{self.predictor.version}:backfill",
                p.model_margin, p.model_total, p.fair_margin, p.fair_total,
                p.home_win_prob, p.market_spread, p.market_total, p.spread_edge,
                p.total_edge, json.dumps(p.components),
            ])
        db.executemany(
            "INSERT OR REPLACE INTO predictions"
            "(game_id, captured_at, model_version, margin_home, total_points,"
            " fair_margin, fair_total, home_win_prob,"
            " market_spread, market_total, spread_edge, total_edge, components) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

        # ---- season simulation
        margins = {gid: p.fair_margin for gid, p in by_game.items()}
        win_totals = db.get_meta("season_win_totals", {}) or {}
        # Two passes, so the season-long odds are a blend rather than the
        # model talking to itself. The first is cheap and exists only to ask
        # where our ratings land each team; the ratings are then pulled part
        # of the way toward the market's posted win totals, and the real run
        # replays the season from there. See anchor_to_market.
        scout = simulate_season(season, games, power, game_margins=margins,
                                n_sims=2000)
        power = anchor_to_market(
            power, {t: s.exp_wins for t, s in scout.teams.items()}, win_totals)
        sim = simulate_season(season, games, power, game_margins=margins, n_sims=20000)
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

        # The week's ranking, frozen. Written *here*, after the simulation,
        # rather than beside the ratings above: the Teams page orders teams by
        # a blend that includes projected wins and title odds, and a history
        # ordered by the rating alone would report movement between two tables
        # that were never using the same rule. While this week is current the
        # snapshot is refreshed on every recompute; once the week turns over
        # nothing writes to it again.
        with contextlib.suppress(Exception):
            self.store_power_snapshot(season, week, power, completed,
                                      projections=_projection_rows(sim))

        # And the weeks before this one, which an install made mid-season has
        # never seen. Reconstructed from the games that had finished before
        # each of them, so the history is complete from the first launch rather
        # than starting at whatever week someone happened to install in.
        #
        # Cheap in the only case that matters: it writes nothing for a week
        # already held, so the work is done once and every later recompute
        # finds nothing to do. Suppressed because a ranking history is not
        # worth failing a recompute over.
        with contextlib.suppress(Exception):
            self.rebuild_power_history(season, completed=completed)

        # ---- picks
        self._store_picks(season, week, games, by_game, consensus)

        # ---- grading
        from .backtest.grade import grade_completed_games

        graded = grade_completed_games(season)

        db.set_meta("last_recompute", stamp)
        # Alerts are derived from the same cards the dashboard renders, rather
        # than from a second set of queries, so an alert can never describe
        # something the board disagrees with.
        raised = 0
        try:
            from . import alerts as alerts_module
            from .api import game_cards

            raised = alerts_module.record(alerts_module.from_games(
                game_cards(season, week)))
        except Exception as exc:  # noqa: BLE001 - alerts must never break a refresh
            log.warning("could not raise alerts: %s", exc)

        detail = (
            f"{len(predictions)} predictions, {len(sim.teams)} team projections, "
            f"{graded} newly graded"
            + (f", {raised} new alert(s)" if raised else "")
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
        survivor = plan_survivor(season, week, by_week, used_teams=used).to_dict()

        comparisons = self._prediction_market_view(week, upcoming, consensus, by_game)

        for contest, payload in (
            ("ats", {"edges": edges}),
            ("prediction_markets", {"games": comparisons}),
            ("pickem", boards),
            ("survivor", survivor),
        ):
            db.execute(
                "INSERT OR REPLACE INTO pick_history(contest, season, week, captured_at, payload) "
                "VALUES(?,?,?,?,?)",
                (contest, season, week, stamp, json.dumps(payload)),
            )

    # ------------------------------------------------- weekly power history
    def _records_through(self, completed: list[dict], season: int) -> dict[str, list[float]]:
        """Wins, losses and ties per team, for the snapshot's record column."""
        out: dict[str, list[float]] = {}
        for g in (x for x in completed if int(x["season"]) == int(season)):
            hs, as_ = g.get("home_score"), g.get("away_score")
            if hs is None or as_ is None:
                continue
            for team in (g["home"], g["away"]):
                out.setdefault(team, [0.0, 0.0, 0.0])
            if float(hs) == float(as_):
                out[g["home"]][2] += 1
                out[g["away"]][2] += 1
            else:
                winner, loser = ((g["home"], g["away"]) if float(hs) > float(as_)
                                 else (g["away"], g["home"]))
                out[winner][0] += 1
                out[loser][1] += 1
        return out

    def store_power_snapshot(self, season: int, week: int, power, completed: list[dict],
                             *, source: str = "live",
                             projections: dict | None = None) -> int:
        """Freeze one week's power ranking.

        Ordered by the same blend the Teams page uses when the projections for
        that week are to hand, and by the rating when they are not. That split
        is not a fudge, it is the only honest reading: a week we were running
        for has its simulation, and a week reconstructed afterwards cannot --
        projected finish comes out of twenty thousand replays of a schedule
        that has since been played. Rebuilt rows are marked as such.

        A live row is never overwritten by a rebuilt one. The reverse is fine:
        a reconstruction is a stand-in until the real thing exists.
        """
        existing = {
            r["team"]: r["source"] for r in db.query(
                "SELECT team, source FROM power_snapshots WHERE season = ? AND week = ?",
                (season, week),
            )
        }
        if source == "rebuilt" and any(v == "live" for v in existing.values()):
            return 0

        records = self._records_through(completed, season)
        ranked = self._rank_for_snapshot(power, projections)
        stamp = now_iso()
        rows = []
        for rank, team in enumerate(ranked, start=1):
            wins, losses, ties = records.get(team.team, [0.0, 0.0, 0.0])
            rows.append([season, week, team.team, rank, team.power, team.elo,
                         team.pythagorean, wins, losses, ties, source, stamp])
        db.executemany(
            "INSERT OR REPLACE INTO power_snapshots"
            "(season, week, team, rank, power, elo, pythagorean,"
            " wins, losses, ties, source, captured_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)

    @staticmethod
    def _rank_for_snapshot(power, projections: dict | None) -> list:
        """Best first, by the blend where it can be and the rating otherwise."""
        teams = list(power.teams.values())
        if projections:
            from .api import ranking_scores

            ratings = {t.team: {"power": t.power, "pythagorean": t.pythagorean}
                       for t in teams}
            scores = ranking_scores(ratings, projections)
            return sorted(teams, key=lambda t: (-scores.get(t.team, 0.0), t.team))
        return sorted(teams, key=lambda t: (-(t.power or 0.0), t.team))

    def rebuild_power_history(self, season: int | None = None, *,
                              completed: list[dict] | None = None,
                              overwrite: bool = False) -> dict:
        """Reconstruct the weekly rankings for weeks we were not running for.

        Week 1 of a season the app was installed halfway through has no live
        snapshot and never will. It does not need to be imported from anywhere:
        the rating is a function of the games that had finished by then, and
        those are in the database. So each missing week is recomputed from the
        games completed before it -- exactly the freeze-after-week-W procedure
        the rating's own constants were fitted with.

        Marked 'rebuilt', and deliberately not passed off as a record. It is
        what today's rating code says about that week, which equals what was on
        screen at the time only if the rating has not changed since -- and it
        has, twice, this season alone.
        """
        season = season or self.season()
        # recompute() has already loaded exactly this list, and on the common
        # path -- every week already held -- re-reading the whole game history
        # is the only work this function would do.
        if completed is None:
            completed = db.query(
                "SELECT * FROM games WHERE status = 'final' AND season <= ? "
                "ORDER BY season, week, kickoff",
                (season,),
            )
        if not completed:
            return {"season": season, "weeks": [], "reason": "no completed games"}

        have = {
            r["week"]: r["source"] for r in db.query(
                "SELECT week, MIN(source) AS source FROM power_snapshots "
                "WHERE season = ? GROUP BY week", (season,),
            )
        }
        played = sorted({int(g["week"]) for g in completed
                         if int(g["season"]) == int(season)})
        if not played:
            return {"season": season, "weeks": [], "reason": "no completed games this season"}

        # The last week the schedule actually has. Without this the season's
        # final week produces a ranking for the week after it, which does not
        # exist -- a week 19 row for an 18-week season, indistinguishable on
        # screen from a real one.
        scheduled = db.query(
            "SELECT MAX(week) AS last FROM games WHERE season = ? AND season_type = 'REG'",
            (season,),
        )
        last_week = (scheduled[0]["last"] if scheduled else None) or max(played)

        written: list[int] = []
        # Through the last week that has finished, plus the one after it: a
        # ranking for week N is the state going *into* week N, so the week
        # after the last completed one is the first that is still live.
        for week in range(1, min(max(played) + 1, last_week) + 1):
            if not overwrite and have.get(week) == "live":
                continue
            if not overwrite and week in have:
                continue
            # Only what had finished before this week kicked off. Including the
            # week's own results would rank teams by a game they had not played
            # yet, which is the one mistake a history like this can make.
            before = [g for g in completed
                      if int(g["season"]) < int(season) or int(g["week"]) < week]
            if not any(int(g["season"]) == int(season) for g in before) and week > 1:
                continue
            elo = run_elo(before)
            power = self.power_from(before, elo, season, week=week)
            if self.store_power_snapshot(season, week, power, before, source="rebuilt"):
                written.append(week)
        return {"season": season, "weeks": written}

    def power_from(self, completed: list[dict], elo, season: int, *, week: int):
        """Power ratings from a set of finished games.

        Shared by the model stage and the backfill so the two cannot drift.
        Records are this season's points for and against, which is what the
        Pythagorean term is built from -- `completed` deliberately spans every
        season so Elo carries over, and summing a team's whole career here
        would make the term a constant that never moves.
        """
        epa_meta = db.get_meta("team_epa", {}) or {}
        efficiencies = (
            from_epa_frame(pd.DataFrame(epa_meta["rows"]))
            if epa_meta.get("season") == season and epa_meta.get("rows") else {}
        )
        records: dict[str, list[float]] = {}
        played: dict[str, int] = {}
        win_loss: dict[str, list[int]] = {}
        for g in (x for x in completed if int(x["season"]) == int(season)):
            hs, as_ = g.get("home_score"), g.get("away_score")
            if hs is None or as_ is None:
                continue
            for team, scored, allowed in ((g["home"], hs, as_), (g["away"], as_, hs)):
                bucket = records.setdefault(team, [0.0, 0.0])
                bucket[0] += float(scored)
                bucket[1] += float(allowed)
                played[team] = played.get(team, 0) + 1
            # Ties count as neither, which is what a 0.5 win rate would say
            # anyway and keeps the arithmetic honest about a rare case.
            if float(hs) != float(as_):
                winner, loser = ((g["home"], g["away"]) if float(hs) > float(as_)
                                 else (g["away"], g["home"]))
                win_loss.setdefault(winner, [0, 0])[0] += 1
                win_loss.setdefault(loser, [0, 0])[1] += 1
        # The quarterback each team has actually been playing. The registry is
        # keyed by normalised name and its depth list is most-recent-starter
        # first, so the head of that list is who the team last went out with --
        # which is what the rating's coefficient was fitted against.
        #
        # Not who is expected to start on Sunday: an announced change is priced
        # by the availability layer a few lines further on, and charging for
        # the same absence in both places would double-count the most
        # expensive injury in the sport.
        qb_value: dict[str, float] = {}
        with contextlib.suppress(Exception):
            values, qb_depth = self.quarterback_registry(season)
            for team, passers in qb_depth.items():
                value = values.get(passers[0]) if passers else None
                if value is not None:
                    qb_value[team] = float(value)

        return build_power_ratings(
            elo.as_points(), efficiencies, week=week, elo_raw=elo.snapshot(),
            records={t: (pf, pa) for t, (pf, pa) in records.items()},
            games_played=played,
            win_loss={t: (w, losses) for t, (w, losses) in win_loss.items()},
            qb_value=qb_value,
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
        # The same enrichment the model stage does. Without the forecast every
        # game looks like a still, temperate one, and the total model -- which
        # has little else to separate two games -- returns near enough the same
        # number for all of them.
        weather = self.load_weather()
        for g in history:
            row = consensus.get(g["game_id"])
            if row:
                g["spread_home"] = row["spread_home"]
                g["market_total"] = row["total_points"]
            forecast = weather.get(g["game_id"])
            if forecast and g.get("temp") is None:
                g["temp"] = forecast["temp_f"]
                g["wind"] = forecast["wind_mph"]
            if forecast and forecast["indoor"]:
                g["roof"] = forecast["roof"] or g.get("roof")

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
        # market components are the ones that carry the per-game timing. Built
        # the same way the model stage builds them -- this used to skip the
        # efficiency and record inputs, and while the power *margin* is
        # Elo-driven and survives that, the *total* is one offence against the
        # other defence: with nothing to separate them every team came out
        # league-average and every game projected the same 45 points.
        finals = [g for g in history if g["status"] == "final"]
        elo = run_elo(finals)
        power = self.power_from(finals, elo, season, week=18)
        predictions = self.predictor.predict_frame(frame, power)
        version = f"{self.predictor.version}:backfill"
        stamp = now_iso()
        db.executemany(
            "INSERT OR REPLACE INTO predictions"
            "(game_id, captured_at, model_version, margin_home, total_points,"
            " fair_margin, fair_total, home_win_prob,"
            " market_spread, market_total, spread_edge, total_edge, components) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                [p.game_id, stamp, version, p.model_margin, p.model_total,
                 p.fair_margin, p.fair_total, p.home_win_prob,
                 p.market_spread, p.market_total, p.spread_edge, p.total_edge,
                 json.dumps(p.components)]
                for p in predictions
            ],
        )
        return len(predictions)

    def _prediction_market_view(self, week: int, upcoming: list[dict],
                                consensus: dict, by_game: dict) -> list[dict]:
        """Prediction-market prices beside the books, for display only.

        Produces no recommendation and feeds no model — it is a second opinion
        rendered next to the first.
        """
        from .market.prediction_markets import VENUES, build_comparisons

        games = [g for g in upcoming if int(g["week"]) == week]
        if not games:
            return []
        ids = [g["game_id"] for g in games]
        placeholders = ",".join("?" for _ in ids)

        venue_quotes: dict[str, dict[str, dict]] = {}
        for venue in VENUES:
            rows = db.query(
                "SELECT o.game_id, o.home_price, o.away_price, o.captured_at "
                "FROM odds_snapshots o JOIN (SELECT game_id, MAX(captured_at) AS m "
                "  FROM odds_snapshots WHERE book = ? AND market = 'moneyline' "
                f"  AND game_id IN ({placeholders}) GROUP BY game_id) x "
                "ON x.game_id = o.game_id AND x.m = o.captured_at "
                "WHERE o.book = ? AND o.market = 'moneyline'",
                [venue, *ids, venue],
            )
            if rows:
                venue_quotes[venue] = {r["game_id"]: r for r in rows}
        if not venue_quotes:
            return []

        predictions = {
            gid: {"home_win_prob": p.home_win_prob} for gid, p in by_game.items()
        }
        return [
            c.to_dict()
            for c in build_comparisons(games, consensus, venue_quotes, predictions)
        ]

    # ------------------------------------------------------------ full refresh
    def last_success(self, stage: str) -> float | None:
        """Seconds since this stage last completed successfully, if ever."""
        row = db.query_one(
            "SELECT ts FROM fetch_log WHERE source = ? AND ok = 1 "
            "ORDER BY id DESC LIMIT 1",
            (stage,),
        )
        if not row or not row["ts"]:
            return None
        return seconds_since(row["ts"])

    def refresh(self, stages: list[str] | None = None, *,
                force_odds: bool = False) -> RefreshResult:
        """Run the requested stages in registry order.

        Stages are driven from :mod:`nflpicker.stages` rather than a list
        written out here, so adding a source does not mean remembering to edit
        this method, the scheduler, and the default list in two places.

        **Naming stages forces them; asking for everything does not.** A
        refresh with no stage list means "catch up on whatever is due", and a
        stage polled more recently than its own interval is skipped.

        This used to re-run every stage on every call, which made the two
        things a refresh is asked for most expensive. Each press of the refresh
        button, and each launch of the app, spent three Odds API credits on a
        line that had not moved -- enough presses in an afternoon to exhaust a
        day's budget on nothing -- and re-downloaded the nflverse play-by-play
        release, which is most of why a refresh took as long as it did. Both
        already had intervals; nothing consulted them outside the scheduler.
        """
        from .stages import STAGES, stage_names

        explicit = bool(stages)
        wanted = set(stages) if stages else set(stage_names(self.config))
        result = RefreshResult()

        for stage in STAGES:
            if stage.name not in wanted:
                continue
            method = getattr(self, stage.method_name(), None)
            if method is None:
                result.record(stage.name, False, "no handler registered")
                continue

            if not explicit and not stage.always:
                age = self.last_success(stage.name)
                interval = stage.interval(self.config)
                if age is not None and age < interval:
                    # Recorded as a success, because it is one: the stored data
                    # is current by this stage's own definition of current.
                    # Reporting it as a failure would train the reader to
                    # ignore the one strip that tells them a source is broken.
                    result.record(
                        stage.name, True,
                        f"already current — fetched {_ago(age)} ago, "
                        f"next due in {_ago(interval - age)}",
                        skipped=True,
                    )
                    continue

            try:
                if stage.name == "odds":
                    method(result, force=force_odds)
                else:
                    method(result)
            except Exception as exc:  # noqa: BLE001 - one stage must not stop the rest
                result.record(stage.name, False, str(exc))

        result.finished_at = now_iso()
        db.set_meta("last_refresh", result.to_dict())
        return result

    def refresh_recompute(self, result: RefreshResult) -> None:
        """Registry-facing name for the analytical pass."""
        self.recompute(result)

    def purge_demo_data(self) -> int:
        """Remove synthetic games left behind by a previous demo run.

        Demo games are stored in the same tables as real ones, distinguished
        only by a ``demo-`` id prefix. Nothing removed them, so a data directory
        that had ever been opened in demo mode kept a synthetic slate mixed in
        with the real one forever -- alongside real games, in the same week, on
        the same board.

        The tables are discovered from the schema rather than listed here. A
        hardcoded list is exactly the kind that goes stale the next time a
        table keyed on ``game_id`` is added, leaving demo rows behind in the
        one place nobody remembered to update.
        """
        conn = db.connect()
        removed = 0
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall():
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "game_id" not in columns:
                continue
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE game_id LIKE 'demo-%'")  # noqa: S608
            removed += cursor.rowcount or 0
        conn.commit()
        if removed:
            log.info("removed %d synthetic row(s) left by a previous demo run", removed)
        return removed

    def stored_seasons(self) -> list[int]:
        """Every season with games on file, oldest first."""
        return [int(r["season"]) for r in db.query(
            "SELECT DISTINCT season FROM games ORDER BY season")]

    def backfill_season(self, season: int) -> int:
        """Fetch and store one season's schedule and results.

        The refresh loop only ever asks for the current season, so a season the
        app was not running for is simply absent — which is why the board could
        not look backwards at all. This fills one in on demand.

        Results only. Odds are not backfilled and cannot be: a line is a
        snapshot of what was on offer at a moment, and nobody sells the past.
        """
        if self.demo:
            from .sources.demo import _demo_season

            return self.upsert_games(_demo_season(season, 18)["games"])

        from .sources.espn import EspnSource

        games = EspnSource().season_schedule(season)
        if not games:
            raise SourceError(f"ESPN returned no games for {season}")
        stored = self.upsert_games(games)
        db.log_fetch("backfill", True, f"{season}: {stored} games")
        return stored

    def bootstrap(self) -> RefreshResult:
        """First run: make sure the app has something to show."""
        if not self.demo:
            self.purge_demo_data()
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

    from .sources.kalshi import KalshiQuote
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
        # Thin venues that lag: mostly close to the books, occasionally well
        # off, and not identical to each other.
        for venue in ("polymarket", "kalshi"):
            drift = rng.gauss(0, 0.03) + (
                rng.choice([-0.09, 0.09]) if rng.random() < 0.20 else 0
            )
            price = min(0.97, max(0.03, float(fair) + drift))
            if venue == "polymarket":
                quotes.append(PolymarketQuote(
                    home=game["home"], away=game["away"],
                    home_price=price, away_price=1 - price,
                    kickoff=game["kickoff"], market_id=f"demo-pm-{game['game_id']}",
                    volume=rng.uniform(2000, 60000),
                ))
            else:
                quotes.append(KalshiQuote(
                    home=game["home"], away=game["away"],
                    home_price=price, away_price=1 - price,
                    kickoff=game["kickoff"],
                    event_ticker=f"demo-kalshi-{game['game_id']}",
                    volume=rng.uniform(2000, 60000),
                ))
    return quotes


def _demo_live_games(games: list[dict], week: int) -> list[dict]:
    """Put a couple of the current week's games in progress, for demo mode."""
    import random

    from .live import seconds_remaining

    upcoming = [g for g in games if g["week"] == week and g["status"] == "scheduled"]
    if len(upcoming) < 2:
        return games

    rng = random.Random(week * 97)
    chosen = {g["game_id"] for g in upcoming[:2]}
    out = []
    for game in games:
        if game["game_id"] not in chosen:
            out.append(game)
            continue
        period = rng.choice([2, 3, 4])
        clock = rng.uniform(60, 880)
        home_score = rng.choice([7, 10, 13, 17, 20, 24])
        away_score = rng.choice([3, 7, 14, 17, 21])
        yard_line = rng.randint(5, 95)
        out.append({
            **game,
            "status": "in_progress",
            "home_score": home_score,
            "away_score": away_score,
            "live": {
                "period": period,
                "clock": f"{int(clock // 60)}:{int(clock % 60):02d}",
                "seconds_left": seconds_remaining(period, clock),
                "possession": rng.choice([game["home"], game["away"]]),
                "down": rng.randint(1, 4),
                "distance": rng.randint(1, 15),
                "yard_line": yard_line,
                # Consistent with field position rather than rolled separately.
                "red_zone": yard_line >= 80,
                "home_timeouts": rng.randint(0, 3),
                "away_timeouts": rng.randint(0, 3),
                "last_play": "Synthetic demo drive — enable live sources for real plays.",
                "detail": f"Q{period}",
            },
        })
    return out


def _projection_rows(sim) -> dict:
    """The simulation as the shape `ranking_scores` reads."""
    return {
        t.team: {"exp_wins": t.exp_wins, "wins_p10": t.wins_p10, "sb_prob": t.sb_prob}
        for t in sim.teams.values()
    }


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
