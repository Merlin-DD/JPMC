"""Deferred, idempotent startup: database bootstrap + the scheduler.

Neither the bootstrap sequence nor the scheduler's background threads may
run in AppConfig.ready(). Render's gunicorn.conf.py sets preload_app=True,
so ready() executes in the gunicorn MASTER process, before any worker is
forked — and SQLite's own documentation is explicit that a connection must
never be carried across fork(): the child inherits a copy of the file
descriptor, sharing the parent's OS-level lock state, but not the thread
that would ever release it. Two distinct failures follow from that:

* A ready()-time bootstrap opens and closes several connections in the
  master. Individually closed, that's not the danger by itself — the
  scheduler threads ready() used to start are: they keep the master
  opening and closing connections indefinitely, on an ongoing 60s/900s
  cadence, for as long as that master process lives. If gunicorn later
  forks a *replacement* worker (crash recovery, request-count recycling)
  at the exact moment one of those threads holds a connection mid-write,
  the new worker inherits a permanently locked or corrupted database and
  every request against it fails from then on — with nothing useful in
  the traceback beyond "database is locked" or "disk image is malformed".
* Threads spawned pre-fork simply don't exist post-fork at all — fork()
  only clones the calling thread — so the scheduler that ready() started
  in the master is silently gone in every actual worker.

The fix: book/apps.py's ready() does not open a connection or start a
thread. It only registers a trigger. `ensure_started()` here is the real
entry point — idempotent and non-blocking, safe to call multiple times,
concurrently, from multiple places — and it does the actual work on a
background thread so its caller is never held up by it. Two callers fire
it, and either one winds up doing the work; the other is a no-op:

* gunicorn.conf.py's `post_fork` hook — the primary path in production.
  Runs once per worker, in that worker's own process, strictly after its
  fork: the boundary SQLite asks for.
* Django's `request_started` signal (registered in book/apps.py) — the
  fallback for `manage.py runserver`, which never forks and has no
  post_fork hook to call. Also defense in depth if the gunicorn hook is
  ever not picked up.
"""

import logging
import os
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_started = False


def ensure_started() -> None:
    """Idempotent and non-blocking. However many times, from however many
    places, this is called — a post_fork hook, a request, both racing
    each other — the real work happens at most once per process, and the
    caller never waits on it."""
    global _started
    if _started:
        return
    with _lock:
        if _started:
            return
        _started = True

    threading.Thread(target=_run_once, name="synth-pnl-startup", daemon=True).start()


def _run_once() -> None:
    from book.scheduler import should_start

    if not should_start():
        logger.info("startup: not running under this process (see scheduler guard)")
        return

    try:
        _bootstrap()
    except Exception:
        logger.exception(
            "startup: bootstrap failed — the process keeps serving requests and "
            "/healthz still answers; the scheduler will keep idling and retrying "
            "until the database is fixed (or run `python manage.py bootstrap` by hand)"
        )

    # Started regardless of whether bootstrap succeeded: its own
    # missing-schema check is exactly the degraded-mode safety net for
    # when bootstrap fails, not something that should also be skipped.
    from book import scheduler

    scheduler.start()


def _bootstrap() -> None:
    _ensure_db_directory()
    _bootstrap_schema_and_seed()
    _compute_attribution_once()
    _generate_commentary_once()


def _ensure_db_directory() -> None:
    """On a fresh Render disk the mount point exists but DB_PATH's parent
    may not — sqlite3.connect() does not create directories, only the
    file itself, so this has to happen before the first get_conn()."""
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
