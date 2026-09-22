"""SQLite storage.

Everything the app learns is append-only snapshots keyed by ``captured_at``:
that is what makes "show me how this line moved" and "what did the model think
on Tuesday" answerable later.  Current-state queries just take the latest
snapshot per key.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import get_config

log = logging.getLogger("nflpicker.db")

SCHEMA_VERSION = 13

# Columns added to tables that already shipped, as (table, column, declaration).
# Adding a column to SCHEMA alone does nothing to a database that already has
# the table, so every such change is recorded here too and applied by migrate().
# Entries stay forever: they are how a database from any older version catches
# up, and each one is a no-op once applied.
COLUMN_ADDITIONS: tuple[tuple[str, str, str], ...] = (
    # v6: the blended estimate is what home_win_prob is built from and what the
    # board reports as "ours", but only the market-blind margin was ever stored
    # -- so the spread on screen and the win probability beside it came from two
    # different numbers.
    ("predictions", "fair_margin", "REAL"),
    ("predictions", "fair_total", "REAL"),
    # v8: the power rating is now shrunk Elo plus Pythagorean expectation, and
    # the Pythagorean term is shown on the Teams page as the reason a team is
    # rated above or below its record.
    ("team_ratings", "pythagorean", "REAL"),
    # v11: the injury report is read for four things -- who, what, how long,
    # and how serious. Only "who" and "how serious" were columns; the other two
    # were buried in a 400-character prose comment that had to be read to be
    # understood, which is not what a table is for.
    # v12: a week's ranking now carries the projection it was ordered by, and
    # the record it was taken with. The order was frozen and everything beside
    # it on the row was live, so week 2's table showed a 2-0 team -- the games
    # played on the Thursday of the week the ranking was supposed to precede.
    ("power_snapshots", "projection", "TEXT"),
    ("injuries", "injury", "TEXT"),
    ("injuries", "return_date", "TEXT"),
    ("injuries", "first_seen", "TEXT"),
)

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
    margin_home    REAL NOT NULL,       -- market-blind model: home points - away points
    total_points   REAL NOT NULL,       -- market-blind model total
    fair_margin    REAL,                -- model blended with the market: our actual estimate
    fair_total     REAL,
    home_win_prob  REAL NOT NULL,       -- derived from fair_margin, not margin_home
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
    pythagorean REAL,                   -- win expectation from points for/against
    off_rating  REAL,
    def_rating  REAL,
    UNIQUE(team, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_ratings_team ON team_ratings(team, captured_at);

-- The power ranking as it stood in a given week, one row per team per week.
--
-- team_ratings already keeps a history, but keyed by capture timestamp: it
-- answers "what did we think at 14:07 on Tuesday", which nobody asks. This
-- answers "where did this team rank in week 6", which is the question a
-- ranking history exists for, and it is a different shape -- one frozen row
-- per week, carrying the rank itself rather than only the inputs to it.
--
-- `source` separates a snapshot taken while that week was live ('live') from
-- one reconstructed afterwards from the completed games ('rebuilt'). They are
-- not the same claim: a rebuilt row is what today's rating code says about
-- that week, which is only what was on screen at the time if the rating has
-- not changed since. Keeping them apart is what stops a reconstruction from
-- quietly becoming a record.
CREATE TABLE IF NOT EXISTS power_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    team        TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    power       REAL,
    elo         REAL,
    pythagorean REAL,
    wins        REAL,
    losses      REAL,
    ties        REAL,
    source      TEXT NOT NULL DEFAULT 'live',
    captured_at TEXT NOT NULL,
    projection  TEXT,                   -- json of the projection it was ordered by
    UNIQUE(season, week, team)
);
CREATE INDEX IF NOT EXISTS idx_power_snap ON power_snapshots(season, week, rank);

-- Published power rankings from other outlets, one row per team per list.
-- Kept whole: a partial list is rejected before it reaches here, so anything
-- stored is a complete 1-32 and the consensus cannot be dragged by a parser
-- that only half-worked.
CREATE TABLE IF NOT EXISTS external_rankings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    team        TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE(source, season, week, team)
);
CREATE INDEX IF NOT EXISTS idx_extrank ON external_rankings(season, week);

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
    detail      TEXT,                   -- the source's prose comment, verbatim
    injury      TEXT,                   -- body part / kind, e.g. "Hamstring"
    return_date TEXT,                   -- expected return, when the source says
    first_seen  TEXT,                   -- when this spell was first reported
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

-- Weekly depth charts, so the replacement for an injured starter is the team's
-- actual backup rather than whoever happens to have started before. Inferring
-- it from past starts fails exactly when it matters: for a backup who has
-- never started.
CREATE TABLE IF NOT EXISTS depth_chart (
    season     INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    team       TEXT NOT NULL,
    position   TEXT NOT NULL,
    depth      INTEGER NOT NULL,
    player     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (season, week, team, position, depth)
);
CREATE INDEX IF NOT EXISTS idx_depth_team ON depth_chart(season, team, position);

-- Your own picks, so the app can compare itself against you rather than only
-- against the market. One row per game per contest; re-picking replaces it.
CREATE TABLE IF NOT EXISTS user_picks (
    season     INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    game_id    TEXT NOT NULL,
    contest    TEXT NOT NULL DEFAULT 'straight',   -- straight|spread
    selection  TEXT NOT NULL,                      -- team abbreviation
    note       TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (season, week, game_id, contest)
);

-- Picks waiting to be shared with the licence server, when the user has said
-- they may be. An outbox rather than a log: a row here is a pick that has not
-- gone yet, and it is deleted the moment it has. One row per pick, so editing
-- a pick before kickoff replaces the queued copy instead of sending a second
-- one -- what the server should hold is the pick as it stands, not a history
-- of somebody changing their mind.
--
-- Nothing reaches this table unless sharing is on and the notice has been
-- acknowledged; see sharing.may_send, which is checked on the way in as well
-- as on the way out.
CREATE TABLE IF NOT EXISTS share_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,            -- winner|spread|total|survivor
    game_id    TEXT NOT NULL,
    season     INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    side       TEXT NOT NULL,
    line       REAL,                     -- the spread as it was when picked
    price      INTEGER,                  -- American odds at that moment
    total_line REAL,
    book_prob  REAL,                     -- the book's win probability then
    picked_at  TEXT NOT NULL,            -- when the user chose
    queued_at  TEXT NOT NULL,
    tries      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(kind, game_id)
);

-- Assistant conversations. In the database rather than the browser so they
-- survive an update, land in a backup, and are still there on a reinstall --
-- localStorage is per-browser-profile and would quietly lose the lot.
CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,        -- user|assistant
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages ON chat_messages(chat_id, id);

-- Things worth noticing while the app is running: a starter ruled out, a line
-- crossing a key number, a steam move. Written by the recompute pass and read
-- by the dashboard; deliberately in-app only, never pushed anywhere.
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,        -- starter|key_number|steam|edge
    severity   TEXT NOT NULL,        -- info|notable|urgent
    game_id    TEXT,
    title      TEXT NOT NULL,
    detail     TEXT,
    -- One row per distinct event. Recompute runs every few minutes and would
    -- otherwise write the same "QB ruled out" alert until kickoff.
    fingerprint TEXT NOT NULL UNIQUE,
    seen       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);

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


def _stored_version(conn: sqlite3.Connection) -> int:
    """Schema version recorded in the database, or 0 for a database with none."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0        # no meta table yet: a brand-new file
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> bool:
    """Add a column to an existing table if it is not already there.

    The schema is written with CREATE TABLE IF NOT EXISTS, which silently does
    nothing when the table exists. That is right for a new table and wrong for
    a new *column*: an upgraded app would query a column its own schema
    declares and the user's database has never had. This is the safe way to add
    one, and it is a no-op on a fresh database that already has it.
    """
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if not existing or column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


def _backup(db_path: Path, from_version: int) -> Path | None:
    """Copy the database aside before changing its shape.

    Migrations here are additive and safe, but "safe" is a claim about code
    that has not run yet against data that cannot be regenerated: line history
    and closing-line value accumulate only while the app is running and cannot
    be backfilled from anywhere. A copy costs a few megabytes once per upgrade.
    """
    if not db_path.exists():
        return None
    target = db_path.with_name(f"{db_path.name}.v{from_version}.backup")
    try:
        # Copy through SQLite rather than the filesystem so an in-flight WAL is
        # checkpointed into the copy instead of being left behind.
        with sqlite3.connect(str(db_path)) as src, sqlite3.connect(str(target)) as dst:
            src.backup(dst)
        return target
    except Exception:  # noqa: BLE001
        log.warning("could not back up %s before migrating", db_path)
        return None


def migrate(conn: sqlite3.Connection, db_path: Path | None = None) -> int:
    """Bring an existing database up to SCHEMA_VERSION, keeping its data.

    Every step must be additive and idempotent. Nothing here drops or rewrites
    a table: an upgrade that loses a season of captured odds is worse than one
    that fails loudly.
    """
    was = _stored_version(conn)
    if was < SCHEMA_VERSION and was > 0 and db_path is not None:
        backup = _backup(db_path, was)
        if backup:
            log.info("backed up database to %s before migrating", backup.name)

    # Columns added to tables that shipped in an earlier version. Listing them
    # here rather than only in SCHEMA is what makes an upgrade in place work.
    #
    # Checked on every connect, not only when the version number has moved.
    # The version gate was the whole bug the last time this went wrong: a column
    # was added to the list and SCHEMA_VERSION was not bumped with it, so every
    # database already at that version skipped the loop and the app queried a
    # column its own schema declared and the user's database had never had.
    # Relying on a person to remember a constant is not a migration strategy,
    # and the loop costs one PRAGMA per table -- against a query that would
    # otherwise throw, that is free.
    for table, column, decl in COLUMN_ADDITIONS:
        if ensure_column(conn, table, column, decl):
            log.info("added %s.%s", table, column)

    if was >= SCHEMA_VERSION:
        return was

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    if was > 0:
        log.info("migrated database from schema %d to %d", was, SCHEMA_VERSION)
    return was


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Thread-local connection; SQLite objects are not shareable across threads."""
    db_path = Path(path) if path else get_config().db_path
    key = str(db_path)
    conns: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    if key not in conns:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # CREATE TABLE IF NOT EXISTS covers a fresh database and any table added
        # since; migrate() covers the rest, which that cannot reach.
        conn.executescript(SCHEMA)
        migrate(conn, db_path)
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
