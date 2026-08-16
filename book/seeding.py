"""Schema creation and seed-CSV loading, shared by `seed` and `bootstrap`.

The two commands differ in intent, not mechanism:

* `seed` is developer-invoked and assertive — it will drop and rebuild a
  table whose columns no longer match schema.sql, and it reloads every CSV.
* `bootstrap` runs on every deploy and is strictly additive — it creates
  what is missing and fills what is empty, and never drops anything.

Both pull their loaders from here so the CSV column lists and upsert SQL
exist in exactly one place. Imports pandas, so this is management-command
territory only — never the request path.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from django.conf import settings

from book.db import SCHEMA_PATH, executemany, fetch_one, get_state, set_state
from book.ingest import is_future_bar

DATA_DIR = Path(settings.BASE_DIR) / "data"

# Tables with a CSV behind them — the only ones that can be reloaded, and
# so the only ones `seed` may safely drop and rebuild on a mismatch.
SEED_TABLES = ("positions", "prices", "fx_rates")


def _noop(_message: str) -> None:
    pass


def expected_columns() -> dict:
    """Column names schema.sql defines for each seed table."""
    ref = sqlite3.connect(":memory:")
    try:
        ref.executescript(SCHEMA_PATH.read_text())
        return {
            table: [row[1] for row in ref.execute(f"PRAGMA table_info({table})")]
            for table in SEED_TABLES
        }
    finally:
        ref.close()


def create_schema(conn) -> None:
    """Apply schema.sql. Every statement is CREATE TABLE IF NOT EXISTS, so
    this is a no-op for tables that already exist and never touches data."""
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def rebuild_mismatched_tables(conn, warn=_noop) -> list[str]:
    """Drop and recreate seed tables whose columns no longer match
    schema.sql. **Destructive** — only `seed` calls this, never `bootstrap`.

    Without it, `CREATE TABLE IF NOT EXISTS` silently leaves an old-shaped
    table in place and later inserts fail or land in the wrong columns.
    """
    rebuilt = []
    for table, expected in expected_columns().items():
        actual = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        if actual and actual != expected:
            warn(
                f"schema mismatch on {table}: has columns {actual}, "
                f"schema.sql expects {expected} — dropping and rebuilding "
                "from CSV instead of reusing stale rows"
            )
            conn.execute(f"DROP TABLE {table}")
            rebuilt.append(table)

    if rebuilt:
        create_schema(conn)
    return rebuilt


def reject_future_bars(df: pd.DataFrame, table: str, warn=_noop) -> pd.DataFrame:
    """Hard guard: a row whose bar_ts is later than now is always a bug (bad
    synthetic timestamp, clock skew, corrupt CSV) — never valid data."""
    now = datetime.now(timezone.utc)
    bar_ts = pd.to_datetime(df["bar_ts"], utc=True)
    is_future = bar_ts.apply(lambda ts: is_future_bar(ts.to_pydatetime(), now))
    if is_future.any():
        rejected = df.loc[is_future, ["bar_ts"]]
        warn(
            f"{table}: rejecting {len(rejected)} row(s) with future bar_ts: "
            f"{rejected['bar_ts'].tolist()}"
        )
    return df.loc[~is_future]


def load_positions(conn, warn=_noop) -> int:
    df = pd.read_csv(DATA_DIR / "positions.csv")
    rows = list(
        df[
            ["ticker", "name", "currency", "shares", "financing_spread_bps", "sector"]
        ].itertuples(index=False, name=None)
    )
    executemany(
        conn,
        """
        INSERT INTO positions (ticker, name, currency, shares, financing_spread_bps, sector)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            name = excluded.name,
            currency = excluded.currency,
            shares = excluded.shares,
            financing_spread_bps = excluded.financing_spread_bps,
            sector = excluded.sector
        """,
        rows,
    )
    return len(rows)


def load_prices(conn, warn=_noop) -> int:
    df = pd.read_csv(DATA_DIR / "prices.csv")
    df = reject_future_bars(df, "prices", warn)
    rows = list(
        df[
            ["ticker", "bar_ts", "fetched_at", "asof_date", "close", "is_stale"]
        ].itertuples(index=False, name=None)
    )
    executemany(
        conn,
        """
        INSERT INTO prices (ticker, bar_ts, fetched_at, asof_date, close, is_stale)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, bar_ts) DO UPDATE SET
            fetched_at = excluded.fetched_at,
            asof_date = excluded.asof_date,
            close = excluded.close,
            is_stale = excluded.is_stale
        """,
        rows,
    )
    return len(rows)


def load_fx(conn, warn=_noop) -> int:
    df = pd.read_csv(DATA_DIR / "fx.csv")
    df = reject_future_bars(df, "fx_rates", warn)
    rows = list(
        df[
            ["currency", "bar_ts", "fetched_at", "asof_date", "usd_per_unit", "is_stale"]
        ].itertuples(index=False, name=None)
    )
    executemany(
        conn,
        """
        INSERT INTO fx_rates (currency, bar_ts, fetched_at, asof_date, usd_per_unit, is_stale)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(currency, bar_ts) DO UPDATE SET
            fetched_at = excluded.fetched_at,
            asof_date = excluded.asof_date,
            usd_per_unit = excluded.usd_per_unit,
            is_stale = excluded.is_stale
        """,
        rows,
    )
    return len(rows)


LOADERS = {
    "positions": load_positions,
    "prices": load_prices,
    "fx_rates": load_fx,
}


def row_count(conn, table: str) -> int:
    row = fetch_one(conn, f"SELECT COUNT(*) AS n FROM {table}")
    return row["n"] if row else 0


def init_system_state(conn) -> list[str]:
    """Fill in only the keys that are still unset, so this never clobbers
    real fetch history with a fake "just seeded" timestamp."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    defaults = {
        "last_attempt": now_iso,
        "last_successful_fetch": now_iso,
        "last_error": "",
    }
    written = []
    for key, value in defaults.items():
        if get_state(conn, key) is None:
            set_state(conn, key, value)
            written.append(key)
    return written
