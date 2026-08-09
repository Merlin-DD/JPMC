from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand

from book.db import executemany, get_conn

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "schema.sql"
DATA_DIR = Path(settings.BASE_DIR) / "data"


class Command(BaseCommand):
    help = "Create the SQLite schema (if absent) and load the seed CSVs. Safe to re-run."

    def handle(self, *args, **options):
        conn = get_conn()
        try:
            self._create_schema(conn)
            self._load_positions(conn)
            self._load_prices(conn)
            self._load_fx(conn)
        finally:
            conn.close()
        self.stdout.write(self.style.SUCCESS(f"seed complete: {settings.DB_PATH}"))

    def _create_schema(self, conn):
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()

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
        rows = list(
            df[["ticker", "ts", "close", "is_stale"]].itertuples(index=False, name=None)
        )
        executemany(
            conn,
            """
            INSERT INTO prices (ticker, ts, close, is_stale)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker, ts) DO UPDATE SET
                close = excluded.close,
                is_stale = excluded.is_stale
            """,
            rows,
        )

    def _load_fx(self, conn):
        df = pd.read_csv(DATA_DIR / "fx.csv")
        rows = list(
            df[["currency", "ts", "usd_per_unit", "is_stale"]].itertuples(
                index=False, name=None
            )
        )
        executemany(
            conn,
            """
            INSERT INTO fx_rates (currency, ts, usd_per_unit, is_stale)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(currency, ts) DO UPDATE SET
                usd_per_unit = excluded.usd_per_unit,
                is_stale = excluded.is_stale
            """,
            rows,
        )
