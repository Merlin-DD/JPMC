import sqlite3
from pathlib import Path

from django.conf import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def fetch_all(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def fetch_one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def execute(conn, sql, params=()):
    conn.execute(sql, params)
    conn.commit()


def executemany(conn, sql, seq_of_params):
    conn.executemany(sql, seq_of_params)
    conn.commit()


def expected_tables() -> set[str]:
    """Table names schema.sql defines, read by building it in memory rather
    than parsing the .sql text. Stdlib only — the scheduler calls this on
    the server process and must not drag pandas in to do it."""
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(SCHEMA_PATH.read_text())
        rows = ref.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in rows}
    finally:
        ref.close()


def missing_tables(conn) -> list[str]:
    """Which of schema.sql's tables are absent from this database.

    The presence of the *file* proves nothing: sqlite3.connect() creates an
    empty file on first connection, so a database that has only ever been
    opened looks identical to a provisioned one from the filesystem. This
    is the check that actually distinguishes them.
    """
    present = {
        row["name"]
        for row in fetch_all(conn, "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    return sorted(expected_tables() - present)


def get_state(conn, key):
    row = fetch_one(conn, "SELECT value FROM system_state WHERE key = ?", (key,))
    return row["value"] if row else None


def set_state(conn, key, value):
    execute(
        conn,
        """
        INSERT INTO system_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
