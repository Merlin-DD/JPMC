import sqlite3

from django.conf import settings


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
