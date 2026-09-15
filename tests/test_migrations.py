"""Upgrading the app must not cost the user their history.

Line movement and closing-line value accumulate only while the app is running
and cannot be backfilled from anywhere, so an upgrade that drops a table is not
a bug you fix next release — the data is gone.
"""

import sqlite3

import pytest

from nflpicker import db


@pytest.fixture
def old_db(tmp_path):
    """A database as an earlier version left it: real rows, a stale version."""
    path = tmp_path / "nflpicker.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    conn.execute(
        "INSERT INTO games(game_id, season, week, home, away, updated_at) "
        "VALUES('2026_01_KC_BUF', 2026, 1, 'KC', 'BUF', '2026-09-01T00:00:00Z')"
    )
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '1')")
    conn.commit()
    conn.close()
    return path


def test_upgrading_keeps_the_rows(old_db):
    conn = sqlite3.connect(old_db)
    conn.executescript(db.SCHEMA)
    was = db.migrate(conn, old_db)

    assert was == 1
    rows = conn.execute("SELECT game_id FROM games").fetchall()
    assert [r[0] for r in rows] == ["2026_01_KC_BUF"]
    assert db._stored_version(conn) == db.SCHEMA_VERSION


def test_upgrading_backs_the_database_up_first(old_db):
    conn = sqlite3.connect(old_db)
    conn.executescript(db.SCHEMA)
    db.migrate(conn, old_db)

    backup = old_db.with_name(old_db.name + ".v1.backup")
    assert backup.exists(), "an upgrade must leave a copy of what it started from"
    saved = sqlite3.connect(backup).execute("SELECT game_id FROM games").fetchall()
    assert [r[0] for r in saved] == ["2026_01_KC_BUF"]


def test_a_fresh_database_is_not_backed_up(tmp_path):
    """There is nothing to lose yet, and a .backup beside a new install is noise."""
    path = tmp_path / "new.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    assert db.migrate(conn, path) == 0
    assert not list(tmp_path.glob("*.backup"))


def test_migrating_twice_changes_nothing(old_db):
    conn = sqlite3.connect(old_db)
    conn.executescript(db.SCHEMA)
    db.migrate(conn, old_db)
    assert db.migrate(conn, old_db) == db.SCHEMA_VERSION


def test_a_column_added_later_reaches_an_existing_table(tmp_path):
    """The failure this guards against: CREATE TABLE IF NOT EXISTS does nothing
    to a table that exists, so a column added to SCHEMA never appears in a
    database created before it — and the upgraded app queries a column the
    user's file has never had."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE games (game_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO games VALUES('g1')")
    conn.commit()

    assert db.ensure_column(conn, "games", "kickoff", "TEXT") is True
    assert db.ensure_column(conn, "games", "kickoff", "TEXT") is False   # idempotent

    columns = {r[1] for r in conn.execute("PRAGMA table_info(games)")}
    assert "kickoff" in columns
    assert conn.execute("SELECT game_id FROM games").fetchone()[0] == "g1"


def test_every_declared_column_addition_applies_cleanly(tmp_path):
    """Guards the registry itself: a typo in COLUMN_ADDITIONS would otherwise
    only surface on a real user's database during an upgrade."""
    path = tmp_path / "reg.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA)
    for table, column, decl in db.COLUMN_ADDITIONS:
        db.ensure_column(conn, table, column, decl)
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert column in columns, f"{table}.{column} ({decl}) did not apply"


def test_data_survives_closing_and_reopening(tmp_path, monkeypatch):
    """The machine gets turned off. Everything captured so far must still be
    there when it comes back."""
    monkeypatch.setenv("NFLPICKER_DATA_DIR", str(tmp_path))
    from nflpicker.config import reset_config

    reset_config()
    db.close_all()

    db.set_meta("captured", {"lines": 41})
    conn = db.connect()
    conn.execute(
        "INSERT INTO games(game_id, season, week, home, away, updated_at) "
        "VALUES('g', 2026, 2, 'KC', 'BUF', '2026-09-15T00:00:00Z')"
    )
    conn.commit()

    db.close_all()                      # as if the machine were switched off
    reset_config()

    assert db.get_meta("captured") == {"lines": 41}
    assert db.query("SELECT game_id FROM games")[0]["game_id"] == "g"
    db.close_all()
