"""One-time database bootstrap at process start.

Runs from BookConfig.ready(), before the scheduler starts.

Why this exists rather than living in build.sh: on Render, the persistent
disk is mounted at *runtime*, not during the build step. `build.sh` runs
before that mount exists, so `python manage.py bootstrap` there fails with
"unable to open database file" — there is no /data to open DB_PATH under
yet. The build step can only ever prepare code and static files; anything
that touches DB_PATH has to happen after the disk is attached, which means
application startup.

Guarded exactly like book.scheduler — `should_start()` skips every
`manage.py` subcommand except `runserver`'s reloaded child, so this never
fires under `manage.py bootstrap`/`seed`/`shell`/etc. (avoiding a redundant
second bootstrap under the very command that already does this by hand)
and never fires twice under the autoreloader's parent+child.

Never raises past `run()` — a failure here must not stop the process from
booting. /healthz touches neither the database nor any external API
specifically so it can answer 200 even when everything below it is broken;
a startup bootstrap that could take the process down with it would defeat
that. book.scheduler's own missing-schema tolerance (`_schema_ready`)
covers the degraded case: cycles idle and re-check until the database is
fixed, whether that's a retried deploy or a manual `manage.py bootstrap`.
"""

import logging
import os

from django.conf import settings

from book.scheduler import should_start

logger = logging.getLogger(__name__)


def run() -> None:
    if not should_start():
        logger.info("startup: not bootstrapping under this process (see scheduler guard)")
        return

    try:
        _bootstrap()
    except Exception:
        logger.exception(
            "startup: bootstrap failed — the process will still boot and /healthz "
            "will still answer; the scheduler will keep idling and retrying until "
            "the database is fixed (or run `python manage.py bootstrap` by hand)"
        )


def _bootstrap() -> None:
    _ensure_db_directory()
    _bootstrap_schema_and_seed()
    _compute_attribution_once()
    _generate_commentary_once()


def _ensure_db_directory() -> None:
    """On a fresh Render disk the mount point exists but DB_PATH's parent
    may not — sqlite3.connect() does not create directories, only the file
    itself, so this has to happen before the first get_conn()."""
    db_dir = os.path.dirname(settings.DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def _bootstrap_schema_and_seed() -> None:
    from book.db import get_conn
    from book.seeding import bootstrap_database

    conn = get_conn()
    try:
        summary = bootstrap_database(conn, warn=logger.warning)
    finally:
        conn.close()

    if summary["missing_before"]:
        logger.info("startup: created tables: %s", ", ".join(summary["missing_before"]))
    if summary["loaded"]:
        logger.info("startup: seeded: %s", ", ".join(summary["loaded"]))
    if not summary["missing_before"] and not summary["loaded"]:
        logger.info("startup: schema present, left alone")
    if summary["state_written"]:
        logger.info(
            "startup: initialized system_state: %s", ", ".join(summary["state_written"])
        )


def _compute_attribution_once() -> None:
    from book.db import get_conn
    from book.management.commands.compute_attribution import compute_all

    conn = get_conn()
    try:
        summary = compute_all(conn)
    finally:
        conn.close()
    logger.info(
        "startup: attribution rows=%d recon_failures=%d",
        summary["rows_written"],
        summary["recon_failures"],
    )


def _generate_commentary_once() -> None:
    from book.management.commands.generate_commentary import generate_all

    summary = generate_all()
    logger.info(
        "startup: commentary asof=%s action=%s source=%s",
        summary["asof_date"],
        summary["action"],
        summary["source"],
    )
