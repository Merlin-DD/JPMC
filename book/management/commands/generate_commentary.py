"""Generate desk commentary for the book's latest market date.

Thin plumbing around book/commentary.py. `generate_all` is the entry
point the scheduler calls on its own 15-minute cadence.
"""

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand

from book.commentary import (
    COUNT_FOR_DATE,
    EXISTING_COMMENTARY,
    LATEST_ASOF,
    generate,
)
from book.db import fetch_one, get_conn


def generate_all(conn=None, asof_date: str | None = None, force: bool = False) -> dict:
    """Write commentary for `asof_date`, or for the newest attributed date.

    Skips when model commentary already exists for that date. A market
    date's closing marks do not change once the session is over, so
    regenerating would produce different prose describing identical
    numbers — on a control display that reads as a change in the book
    when nothing has changed. The date is the unit of work, not the tick.

    A `fallback` row is *not* a skip: it is a placeholder written because
    the model was unavailable, so a later cycle that can reach the model
    upgrades it in place. `force=True` regenerates regardless.

    Opens its own connection when not given one, so the scheduler can
    call it with no arguments. Never raises — see book/commentary.py.
    """
    owns_conn = conn is None
    conn = conn or get_conn()
    try:
        if asof_date is None:
            row = fetch_one(conn, LATEST_ASOF)
            asof_date = row["asof_date"] if row else None
        if asof_date is None:
            return {
                "asof_date": None,
                "action": "skipped_no_data",
                "source": None,
                "text": None,
                "generated_at": None,
                "error": "no attribution rows in the book",
                "positions": 0,
            }

        if not force:
            existing = fetch_one(conn, EXISTING_COMMENTARY, (asof_date,))
            if existing is not None and existing["source"] == "claude":
                count = fetch_one(conn, COUNT_FOR_DATE, (asof_date,))
                return {
                    "asof_date": asof_date,
                    "action": "skipped_cached",
                    "source": "claude",
                    "text": existing["text"],
                    "generated_at": existing["generated_at"],
                    "error": None,
                    "positions": count["n"] if count else 0,
                }

        return generate(conn, asof_date)
    finally:
        if owns_conn:
            conn.close()


class Command(BaseCommand):
    help = (
        "Generate desk commentary for the latest attributed date (or --asof-date). "
        "Skips dates that already have model commentary; upgrades rule-based rows. "
        "Falls back to a rule-based sentence when the model is unavailable."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--asof-date",
            dest="asof_date",
            default=None,
            metavar="YYYY-MM-DD",
            help="Market date to write commentary for. Defaults to the newest one.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate even when model commentary already exists for the date.",
        )

    def handle(self, *args, **options):
        asof_date = options["asof_date"]
        if asof_date is not None:
            date.fromisoformat(asof_date)

        summary = generate_all(asof_date=asof_date, force=options["force"])
        action = summary["action"]

        self.stdout.write(f"db: {settings.DB_PATH}")
        self.stdout.write(f"  asof_date:  {summary['asof_date']}")
        self.stdout.write(f"  positions:  {summary['positions']}")
        self.stdout.write(f"  source:     {summary['source'] or '—'}")

        if action == "skipped_cached":
            self.stdout.write(
                "  action:     skipped — model commentary already exists for this "
                "date (--force to regenerate)"
            )
        elif action == "unchanged":
            self.stdout.write(
                f"  action:     unchanged — identical to the stored row, "
                f"generated_at left at {summary['generated_at']}"
            )
            if summary["error"]:
                # Still on the fallback path, just not churning the row.
                self.stdout.write(self.style.WARNING(f"  fell back:  {summary['error']}"))
        elif action == "skipped_no_data":
            self.stdout.write(self.style.WARNING(f"  action:     skipped — {summary['error']}"))
        elif summary["error"]:
            # Surfaced rather than swallowed: a book that has quietly been
            # on the fallback path for a week should be obvious here.
            self.stdout.write(self.style.WARNING(f"  action:     fell back — {summary['error']}"))
        elif summary["source"] == "claude":
            self.stdout.write(self.style.SUCCESS("  action:     model commentary written"))

        if summary["text"]:
            self.stdout.write("")
            self.stdout.write(summary["text"])
