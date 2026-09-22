-- Shared picks. Run once against the D1 database bound as PICKS_DB:
--
--   npx wrangler d1 create the-edge-picks
--   # add the returned id to wrangler.toml as the PICKS_DB binding, then
--   npx wrangler d1 execute the-edge-picks --remote --file license-server/schema.sql
--
-- D1 rather than KV, which is what the rest of this Worker uses: a leaderboard
-- is a GROUP BY, and doing that over KV means reading every key on every page
-- load. D1's free tier covers this comfortably -- a hundred customers picking
-- sixteen games a week is under two thousand rows a week.

-- One row per picker per game per kind. A pick that changes before kickoff
-- replaces itself; what is stored is the pick as it stands, with the line the
-- picker actually saw when they made it.
CREATE TABLE IF NOT EXISTS picks (
    picker      TEXT NOT NULL,          -- 16 hex characters, see sharing.py
    game_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- winner|spread|total|survivor
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    side        TEXT NOT NULL,
    line        REAL,
    price       INTEGER,
    total_line  REAL,
    book_prob   REAL,
    picked_at   TEXT,                   -- the client's clock, untrusted
    received_at TEXT NOT NULL,          -- ours, and the one grading believes
    result      TEXT,                   -- win|loss|push, NULL until graded
    graded_at   TEXT,
    PRIMARY KEY (picker, game_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_picks_week ON picks(season, week);
CREATE INDEX IF NOT EXISTS idx_picks_ungraded ON picks(result, season, week);

-- What a picker calls themselves on the leaderboard, and when they were last
-- heard from. Separate from picks so a rename is one row, not thousands.
CREATE TABLE IF NOT EXISTS pickers (
    picker    TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    first_at  TEXT NOT NULL,
    last_at   TEXT NOT NULL
);

-- Finished games, as the grader learned them. Kept so grading is idempotent
-- and so a pick received after kickoff can be recognised and thrown away.
CREATE TABLE IF NOT EXISTS results (
    game_id    TEXT PRIMARY KEY,
    season     INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    kickoff    TEXT,
    home       TEXT NOT NULL,
    away       TEXT NOT NULL,
    home_score INTEGER,
    away_score INTEGER,
    fetched_at TEXT NOT NULL
);
