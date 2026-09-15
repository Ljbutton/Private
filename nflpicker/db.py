"""SQLite storage.

Everything the app learns is append-only snapshots keyed by ``captured_at``:
that is what makes "show me how this line moved" and "what did the model think
on Tuesday" answerable later.  Current-state queries just take the latest
snapshot per key.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import get_config

SCHEMA_VERSION = 4

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    game_id      TEXT PRIMARY KEY,
    season       INTEGER NOT NULL,
    week         INTEGER NOT NULL,
    season_type  TEXT NOT NULL DEFAULT 'REG',
    kickoff      TEXT,                  -- ISO-8601 UTC
    home         TEXT NOT NULL,
    away         TEXT NOT NULL,
    home_score   INTEGER,
    away_score   INTEGER,
    status       TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled|in_progress|final
    neutral_site INTEGER NOT NULL DEFAULT 0,
    roof         TEXT,
    venue        TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_season_week ON games(season, week);
CREATE INDEX IF NOT EXISTS idx_games_kickoff ON games(kickoff);

-- One row per book per market per poll: the raw movement record.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT NOT NULL,
    book        TEXT NOT NULL,
    market      TEXT NOT NULL,          -- spread|total|moneyline
    captured_at TEXT NOT NULL,
    home_point  REAL,                   -- spread: home line. total: total points.
    away_point  REAL,
    home_price  INTEGER,                -- American odds
    away_price  INTEGER,
    UNIQUE(game_id, book, market, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_odds_game ON odds_snapshots(game_id, market, captured_at);

-- Cross-book average, de-vigged, recomputed each poll.
CREATE TABLE IF NOT EXISTS consensus (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id       TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    spread_home   REAL,
    spread_price_home INTEGER,
    spread_price_away INTEGER,
    total_points  REAL,
    total_price_over  INTEGER,
    total_price_under INTEGER,
    ml_home       INTEGER,
    ml_away       INTEGER,
    home_win_prob REAL,                 -- no-vig from moneyline
    n_books       INTEGER NOT NULL DEFAULT 0,
    books         TEXT,                 -- json list
    UNIQUE(game_id, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_consensus_game ON consensus(game_id, captured_at);

-- Our prediction, snapshotted every refresh so history is inspectable.
CREATE TABLE IF NOT EXISTS predictions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id        TEXT NOT NULL,
    captured_at    TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    margin_home    REAL NOT NULL,       -- projected home points - away points
    total_points   REAL NOT NULL,
    home_win_prob  REAL NOT NULL,
    market_spread  REAL,                -- market at time of prediction
    market_total   REAL,
    spread_edge    REAL,                -- our margin vs market spread, home perspective
    total_edge     REAL,
    components     TEXT,                -- json: per-source contributions
    UNIQUE(game_id, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_pred_game ON predictions(game_id, captured_at);

CREATE TABLE IF NOT EXISTS team_ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team        TEXT NOT NULL,
    season      INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    elo         REAL,
    off_epa     REAL,
    def_epa     REAL,
    pace        REAL,
    power       REAL,                   -- points better than average vs neutral opponent
    off_rating  REAL,
    def_rating  REAL,
    UNIQUE(team, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_ratings_team ON team_ratings(team, captured_at);

CREATE TABLE IF NOT EXISTS season_projections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    team          TEXT NOT NULL,
    season        INTEGER NOT NULL,
    captured_at   TEXT NOT NULL,
    wins_actual   REAL,
    losses_actual REAL,
    ties_actual   REAL,
    exp_wins      REAL,
    wins_p10      REAL,
    wins_p90      REAL,
    playoff_prob  REAL,
    division_prob REAL,
    bye_prob      REAL,
    sb_prob       REAL,
    win_total_line REAL,                -- market season win total, if known
    over_prob     REAL,
    distribution  TEXT,                 -- json: win-count histogram
    UNIQUE(team, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_proj_team ON season_projections(team, captured_at);

CREATE TABLE IF NOT EXISTS news (
    id           TEXT PRIMARY KEY,      -- stable hash of source+url/title
    source       TEXT NOT NULL,
    published_at TEXT,
    fetched_at   TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT,
    summary      TEXT,
    teams        TEXT,                  -- json list of abbrs
    players      TEXT,                  -- json list
    category     TEXT,                  -- injury|transaction|suspension|qb|coaching|general
    impact       REAL NOT NULL DEFAULT 0,   -- 0..1 estimated market relevance
    line_impact  REAL,                  -- estimated points of line movement
    seen         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news(published_at DESC);

CREATE TABLE IF NOT EXISTS injuries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team        TEXT NOT NULL,
    player      TEXT NOT NULL,
    position    TEXT,
    status      TEXT,
    detail      TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE(team, player, updated_at)
);
CREATE INDEX IF NOT EXISTS idx_inj_team ON injuries(team, updated_at);

-- Recommended picks snapshotted so "what did you tell me last Thursday" works.
CREATE TABLE IF NOT EXISTS pick_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contest     TEXT NOT NULL,          -- pickem|survivor|ats|total|moneyline
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- json
    UNIQUE(contest, season, week, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_picks ON pick_history(contest, season, week, captured_at);

-- Graded outcomes: how the model actually did, including closing-line value.
CREATE TABLE IF NOT EXISTS graded (
    game_id        TEXT PRIMARY KEY,
    season         INTEGER NOT NULL,
    week           INTEGER NOT NULL,
    graded_at      TEXT NOT NULL,
    actual_margin  REAL,
    actual_total   REAL,
    first_spread   REAL,                -- our earliest market observation
    close_spread   REAL,
    first_total    REAL,
    close_total    REAL,
    pred_margin    REAL,
    pred_total     REAL,
    pred_home_prob REAL,
    ats_pick       TEXT,                -- home|away|none
    ats_result     TEXT,                -- win|loss|push|none
    total_pick     TEXT,                -- over|under|none
    total_result   TEXT,
    su_correct     INTEGER,
    clv_spread     REAL,                -- points of closing-line value captured
    clv_total      REAL,
    brier          REAL,
    log_loss       REAL
);
CREATE INDEX IF NOT EXISTS idx_graded_week ON graded(season, week);

-- Forecast at kickoff, per game. The model trains on temperature and wind from
-- historical records, so without this they arrive as NaN at inference for every
-- upcoming game — trained-on columns that are always empty in production.
CREATE TABLE IF NOT EXISTS game_weather (
    game_id     TEXT PRIMARY KEY,
    updated_at  TEXT NOT NULL,
    roof        TEXT,
    indoor      INTEGER NOT NULL DEFAULT 0,
    temp_f      REAL,
    wind_mph    REAL,
    precip_pct  REAL
);

-- Live in-game state. Its own table rather than columns on `games` so an
-- existing database picks it up with CREATE TABLE IF NOT EXISTS, without a
-- migration to add columns.
CREATE TABLE IF NOT EXISTS live_state (
    game_id        TEXT PRIMARY KEY,
    updated_at     TEXT NOT NULL,
    period         INTEGER,
    clock          TEXT,
    seconds_left   REAL,          -- in the whole game, not the quarter
    possession     TEXT,          -- team abbr with the ball
    down           INTEGER,
    distance       INTEGER,
    yard_line      INTEGER,       -- yards from the possessing team's own goal
    red_zone       INTEGER NOT NULL DEFAULT 0,
    home_timeouts  INTEGER,
    away_timeouts  INTEGER,
    last_play      TEXT,
    detail         TEXT,
    home_score     INTEGER,
    away_score     INTEGER,
    win_prob_home  REAL           -- live, recomputed each poll
);

-- Per-game, per-team detail behind the market-blind features. Kept in its own
-- table rather than recomputed from play-by-play on every refresh, which would
-- mean re-reading hundreds of megabytes of parquet each cycle.
CREATE TABLE IF NOT EXISTS team_game_stats (
    game_id     TEXT NOT NULL,
    team        TEXT NOT NULL,
    season      INTEGER,
    week        INTEGER,
    opponent    TEXT,
    payload     TEXT NOT NULL,   -- json of the extracted stats
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (game_id, team)
);
CREATE INDEX IF NOT EXISTS idx_tgs_season ON team_game_stats(season, week);

CREATE TABLE IF NOT EXISTS fetch_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source    TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    ts        TEXT NOT NULL,
    duration_ms INTEGER,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetchlog_ts ON fetch_log(ts DESC);
"""

_local = threading.local()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Thread-local connection; SQLite objects are not shareable across threads."""
    db_path = Path(path) if path else get_config().db_path
    key = str(db_path)
    conns: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    if key not in conns:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        conns[key] = conn
        _local.conns = conns
    return conns[key]


def close_all() -> None:
    for conn in (getattr(_local, "conns", None) or {}).values():
        conn.close()
    _local.conns = {}


@contextmanager
def transaction(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    conn = conn or connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query(sql: str, params: Iterable[Any] = (), conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or connect()
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def query_one(sql: str, params: Iterable[Any] = (), conn: sqlite3.Connection | None = None) -> dict | None:
    rows = query(sql, params, conn)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = (), conn: sqlite3.Connection | None = None) -> None:
    with transaction(conn) as c:
        c.execute(sql, tuple(params))


def executemany(sql: str, rows: Iterable[Iterable[Any]], conn: sqlite3.Connection | None = None) -> None:
    rows = [tuple(r) for r in rows]
    if not rows:
        return
    with transaction(conn) as c:
        c.executemany(sql, rows)


# ---------------------------------------------------------------- meta helpers

def set_meta(key: str, value: Any, conn: sqlite3.Connection | None = None) -> None:
    payload = value if isinstance(value, str) else json.dumps(value)
    execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, payload), conn)


def get_meta(key: str, default: Any = None, conn: sqlite3.Connection | None = None) -> Any:
    row = query_one("SELECT value FROM meta WHERE key = ?", (key,), conn)
    if row is None:
        return default
    raw = row["value"]
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def log_fetch(source: str, ok: bool, detail: str = "", duration_ms: int = 0) -> None:
    from .util import now_iso

    execute(
        "INSERT INTO fetch_log(source, ok, ts, duration_ms, detail) VALUES(?,?,?,?,?)",
        (source, int(ok), now_iso(), int(duration_ms), detail[:500]),
    )
