"""Make an empty or partial database usable, without disturbing a live one.

For manual use. On Render this same logic also runs automatically at
process startup (book/apps.py) — the persistent disk isn't mounted during
the build step, only at runtime, so build.sh cannot touch DB_PATH and
this command alone is not enough to fix a fresh deploy. See book/startup.py.

The decide-and-do logic lives in book.seeding.bootstrap_database; this
command is only the reporting around it. Nothing here is destructive:
tables from schema.sql that are missing are created, seed CSVs are loaded
only into tables that are empty, and system_state keys are filled in only
where unset. A deploy over a live disk with a month of accumulated marks
is a no-op. Use `seed` when you actually intend to replace the seed data.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from book.db import get_conn
from book.seeding import bootstrap_database


class Command(BaseCommand):
    help = (
        "Create any missing tables and load seed data into empty ones. "
        "Idempotent and non-destructive; safe to run at any time."
    )

    def handle(self, *args, **options):
        warn = lambda message: self.stdout.write(self.style.WARNING(message))  # noqa: E731

        conn = get_conn()
        try:
            self.stdout.write(f"db: {settings.DB_PATH}")
            summary = bootstrap_database(conn, warn)
        finally:
            conn.close()

        if summary["missing_before"]:
            self.stdout.write(f"  schema:     created {', '.join(summary['missing_before'])}")
        else:
            self.stdout.write("  schema:     present, nothing to create")

        if summary["loaded"]:
            self.stdout.write(self.style.SUCCESS(f"  seeded:     {', '.join(summary['loaded'])}"))
        if summary["kept"]:
            self.stdout.write(f"  left alone: {', '.join(summary['kept'])} (already populated)")
        if summary["state_written"]:
            self.stdout.write(f"  state:      initialized {', '.join(summary['state_written'])}")

        self.stdout.write(self.style.SUCCESS("bootstrap complete"))
