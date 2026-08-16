from django.conf import settings
from django.core.management.base import BaseCommand

from book.db import get_conn
from book.seeding import (
    create_schema,
    init_system_state,
    load_fx,
    load_positions,
    load_prices,
    rebuild_mismatched_tables,
)


class Command(BaseCommand):
    help = (
        "Create the SQLite schema and load the seed CSVs, replacing existing "
        "seed rows. Rebuilds a table whose columns no longer match schema.sql. "
        "For deploys use `bootstrap`, which is additive and never drops."
    )

    def handle(self, *args, **options):
        warn = lambda message: self.stdout.write(self.style.WARNING(message))  # noqa: E731

        conn = get_conn()
        try:
            create_schema(conn)
            rebuild_mismatched_tables(conn, warn)
            load_positions(conn, warn)
            load_prices(conn, warn)
            load_fx(conn, warn)
            init_system_state(conn)
        finally:
            conn.close()

        self.stdout.write(self.style.SUCCESS(f"seed complete: {settings.DB_PATH}"))
