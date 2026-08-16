"""Make an empty or partial database usable, without disturbing a live one.

Runs unconditionally on every deploy. The guard it replaces tested for the
database *file*, which does not distinguish a provisioned database from one
that has merely been opened: sqlite3.connect() creates an empty file on
first connection, so once the app had started once the file always existed
and the bootstrap step never ran again — leaving production serving an
empty database.

The check here is on the schema and on row counts, and every action it
takes is additive:

* tables from schema.sql that are missing are created;
* seed CSVs are loaded only into tables that are **empty**;
* system_state keys are filled in only where unset.

Nothing is ever dropped, and no table holding rows is rewritten — so a
deploy over a live disk with a month of accumulated marks is a no-op.
Use `seed` when you actually intend to replace the seed data.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from book.db import get_conn, missing_tables
from book.seeding import LOADERS, create_schema, init_system_state, row_count


class Command(BaseCommand):
    help = (
        "Create any missing tables and load seed data into empty ones. "
        "Idempotent and non-destructive; safe to run on every deploy."
    )

    def handle(self, *args, **options):
        warn = lambda message: self.stdout.write(self.style.WARNING(message))  # noqa: E731

        conn = get_conn()
        try:
            self.stdout.write(f"db: {settings.DB_PATH}")

            missing = missing_tables(conn)
            if missing:
                warn(f"  schema:     missing {', '.join(missing)} — creating")
                create_schema(conn)
                still_missing = missing_tables(conn)
                if still_missing:
                    # Better to fail the deploy than to serve a half-built book.
                    raise RuntimeError(
                        f"tables still missing after applying schema.sql: {still_missing}"
                    )
            else:
                self.stdout.write("  schema:     present, nothing to create")

            loaded, kept = [], []
            for table, loader in LOADERS.items():
                before = row_count(conn, table)
                if before:
                    kept.append(f"{table}={before}")
                    continue
                loader(conn, warn)
                loaded.append(f"{table}={row_count(conn, table)}")

            if loaded:
                self.stdout.write(self.style.SUCCESS(f"  seeded:     {', '.join(loaded)}"))
            if kept:
                self.stdout.write(f"  left alone: {', '.join(kept)} (already populated)")

            written = init_system_state(conn)
            if written:
                self.stdout.write(f"  state:      initialized {', '.join(written)}")
        finally:
            conn.close()

        self.stdout.write(self.style.SUCCESS("bootstrap complete"))
