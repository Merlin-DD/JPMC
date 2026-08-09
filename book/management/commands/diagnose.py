from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from book.db import fetch_all, get_conn


class Command(BaseCommand):
    help = "Print row counts, bar_ts range, staleness split, and system_state for the book."

    def handle(self, *args, **options):
        conn = get_conn()
        try:
            self.stdout.write(f"db: {settings.DB_PATH}\n")
            self._table_counts(conn)
            self._bar_ts_range(conn)
            self._staleness(conn)
            self._newest_bar_age(conn)
            self._system_state(conn)
        finally:
            conn.close()

    def _table_counts(self, conn):
        self.stdout.write(self.style.MIGRATE_HEADING("row counts"))
        for table in ("positions", "prices", "fx_rates"):
            row = fetch_all(conn, f"SELECT COUNT(*) AS n FROM {table}")[0]
            self.stdout.write(f"  {table}: {row['n']}")

    def _bar_ts_range(self, conn):
        self.stdout.write(self.style.MIGRATE_HEADING("bar_ts range"))
        for table in ("prices", "fx_rates"):
            row = fetch_all(
                conn, f"SELECT MIN(bar_ts) AS lo, MAX(bar_ts) AS hi FROM {table}"
            )[0]
            self.stdout.write(f"  {table}: {row['lo']} .. {row['hi']}")

    def _staleness(self, conn):
        self.stdout.write(self.style.MIGRATE_HEADING("is_stale split"))
        for table in ("prices", "fx_rates"):
            rows = fetch_all(
                conn, f"SELECT is_stale, COUNT(*) AS n FROM {table} GROUP BY is_stale"
            )
            counts = {row["is_stale"]: row["n"] for row in rows}
            self.stdout.write(
                f"  {table}: stale={counts.get(1, 0)} fresh={counts.get(0, 0)}"
            )

    def _newest_bar_age(self, conn):
        self.stdout.write(self.style.MIGRATE_HEADING("newest bar_ts age"))
        now = datetime.now(timezone.utc)
        for table in ("prices", "fx_rates"):
            row = fetch_all(conn, f"SELECT MAX(bar_ts) AS hi FROM {table}")[0]
            if row["hi"] is None:
                self.stdout.write(f"  {table}: no rows")
                continue
            newest = datetime.fromisoformat(row["hi"])
            age_minutes = (now - newest).total_seconds() / 60
            self.stdout.write(f"  {table}: {row['hi']} ({age_minutes:.1f} min ago)")

    def _system_state(self, conn):
        self.stdout.write(self.style.MIGRATE_HEADING("system_state"))
        rows = fetch_all(conn, "SELECT key, value FROM system_state ORDER BY key")
        if not rows:
            self.stdout.write("  (empty)")
            return
        for row in rows:
            self.stdout.write(f"  {row['key']} = {row['value']}")
