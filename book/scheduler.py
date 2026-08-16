"""Background refresh loop: fetch a market snapshot, recompute attribution,
then (once it exists) regenerate commentary.

Started once from BookConfig.ready(). Blocked for every `manage.py`
subcommand (migrate, seed, shell, collectstatic, ...) except `runserver`
— gunicorn in production and `manage.py runserver` in dev are the only
entry points that should actually run it.

`runserver`'s autoreloader re-execs the whole command in a child process
with RUN_MAIN=true; the parent is just a file-watcher that also calls
AppConfig.ready() but never serves requests. We only start in the child
(RUN_MAIN=="true") so parent+child don't each spin up a scheduler. Under
`runserver --noreload` there's no child process and RUN_MAIN is never
set, so the scheduler won't start there either — use the normal
(reloading) runserver, or gunicorn, to exercise it locally.
"""

import logging
import os
import sys
import threading
import time

from book.db import get_conn
from book.ingest import fetch_snapshot
from book.management.commands.compute_attribution import compute_all

logger = logging.getLogger(__name__)

REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "60"))

_started = False
_start_lock = threading.Lock()


def _should_start() -> bool:
    argv = sys.argv
    if not argv or os.path.basename(argv[0]) != "manage.py":
        return True  # not a manage.py invocation (e.g. gunicorn) - always eligible

    if len(argv) < 2 or argv[1] != "runserver":
        return False  # any other subcommand: migrate, seed, shell, collectstatic, ...

    return os.environ.get("RUN_MAIN") == "true"


def _recompute_attribution() -> None:
    conn = get_conn()
    try:
        summary = compute_all(conn)
    finally:
        conn.close()
    logger.info(
        "scheduler: attribution rows=%d recon_failures=%d",
        summary["rows_written"],
        summary["recon_failures"],
    )


def _generate_commentary() -> None:
    # Still soft-imported: the commentary module doesn't exist yet, so a
    # missing import is expected rather than an error. Drop the guard the
    # same way the attribution one went once it lands.
    try:
        from book.management.commands.generate_commentary import generate_all
    except ImportError:
        logger.info("scheduler: commentary not implemented yet, skipping")
        return
    generate_all()


def _run_cycle() -> None:
    logger.info("scheduler: cycle start")
    try:
        fetch_snapshot()
    except Exception:
        logger.exception("scheduler: fetch_snapshot failed")

    try:
        _recompute_attribution()
    except Exception:
        logger.exception("scheduler: recompute_attribution failed")

    try:
        _generate_commentary()
    except Exception:
        logger.exception("scheduler: generate_commentary failed")
    logger.info("scheduler: cycle done, next in %ds", REFRESH_SECONDS)


def _run_loop() -> None:
    while True:
        _run_cycle()
        time.sleep(REFRESH_SECONDS)


def start() -> None:
    global _started

    if not _should_start():
        logger.info(
            "scheduler: not starting (argv=%s, RUN_MAIN=%s)",
            sys.argv,
            os.environ.get("RUN_MAIN"),
        )
        return

    with _start_lock:
        if _started:
            logger.info("scheduler: already started, skipping")
            return
        _started = True

    thread = threading.Thread(target=_run_loop, name="synth-pnl-scheduler", daemon=True)
    thread.start()
    logger.info("scheduler: started, refreshing every %ds", REFRESH_SECONDS)
