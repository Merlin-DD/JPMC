import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand

from book.db import executemany, get_conn, get_state, set_state
from book.ingest import is_future_bar

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schema.sql"
DATA_DIR = Path(settings.BASE_DIR) / "data"

# Tables loaded from CSV — the only ones we can safely drop and rebuild on
# a schema mismatch, since there's a source to reload them from.
SEED_TABLES = ("positions", "prices", "fx_rates")


class Command(BaseCommand):
    help = "Create the SQLite schema (if absent) and load the seed CSVs. Safe to re-run."

    def handle(self, *args, **options):
        conn = get_conn()
        try:
            self._create_schema(conn)
            self._load_positions(conn)
            self._load_prices(conn)
            self._load_fx(conn)
            self._init_system_state(conn)
        finally:
            conn.close()
        self.stdout.write(self.style.SUCCESS(f"seed complete: {settings.DB_PATH}"))

    def _expected_columns(self) -> dict:
        """Column names schema.sql defines for each seed table, gotten by
        building it fresh in memory rather than parsing the .sql text."""
        ref_conn = sqlite3.connect(":memory:")
        try:
            ref_conn.executescript(SCHEMA_PATH.read_text())
            return {
                table: [row[1] for row in ref_conn.execute(f"PRAGMA table_info({table})")]
                for table in SEED_TABLES
            }
        finally:
            ref_conn.close()

    def _create_schema(self, conn):
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()

        expected = self._expected_columns()
        rebuilt_any = False
        for table, expected_cols in expected.items():
            actual_cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            if actual_cols and actual_cols != expected_cols:
                self.stdout.write(
                    self.style.WARNING(
                        f"schema mismatch on {table}: has columns {actual_cols}, "
                        f"schema.sql expects {expected_cols} — dropping and "
                        "rebuilding from CSV instead of reusing stale rows"
                    )
                )
                conn.execute(f"DROP TABLE {table}")
                rebuilt_any = True

        if rebuilt_any:
            # Recreate whatever we just dropped; CREATE TABLE IF NOT EXISTS
            # is a no-op for tables that already matched.
            conn.executescript(SCHEMA_PATH.read_text())
            conn.commit()

    def _reject_future_bars(self, df: pd.DataFrame, table: str) -> pd.DataFrame:
        """Hard guard: a row whose bar_ts is later than now is always a
        bug (bad synthetic timestamp, clock skew, corrupt CSV) — never
        valid data. Reject and log it rather than loading it."""
        now = datetime.now(timezone.utc)
        bar_ts = pd.to_datetime(df["bar_ts"], utc=True)
        is_future = bar_ts.apply(lambda ts: is_future_bar(ts.to_pydatetime(), now))
        if is_future.any():
            rejected = df.loc[is_future, ["bar_ts"]]
            self.stdout.write(
                self.style.WARNING(
                    f"{table}: rejecting {len(rejected)} row(s) with future bar_ts: "
                    f"{rejected['bar_ts'].tolist()}"
                )
            )
        return df.loc[~is_future]

    def _load_positions(self, conn):
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

    def _load_prices(self, conn):
        df = pd.read_csv(DATA_DIR / "prices.csv")
        df = self._reject_future_bars(df, "prices")
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

    def _load_fx(self, conn):
        df = pd.read_csv(DATA_DIR / "fx.csv")
        df = self._reject_future_bars(df, "fx_rates")
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

    def _init_system_state(self, conn):
        """Only fills in keys that are still unset, so re-running seed
        against a book that's already had live polling never clobbers
        real fetch history with a fake "just seeded" timestamp."""
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        defaults = {
            "last_attempt": now_iso,
            "last_successful_fetch": now_iso,
            "last_error": "",
        }
        for key, value in defaults.items():
            if get_state(conn, key) is None:
                set_state(conn, key, value)
