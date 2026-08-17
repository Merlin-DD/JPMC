"""Background refresh loops.

Two independent cadences, each on its own daemon thread:

* **refresh** (REFRESH_SECONDS, default 60) — fetch a market snapshot,
  then recompute attribution incrementally.
* **commentary** (COMMENTARY_SECONDS, default 900) — regenerate the
  written commentary. Separate because commentary is expensive (an LLM
  call) and has no reason to run at market-data speed.

Both are started together by `start()`, behind a single guard, so
"started" is all-or-nothing: there is never a book with a refresh loop
but no commentary loop. `start()` itself is called from book/startup.py's
`ensure_started()`, post-fork (gunicorn's post_fork hook, or the first
request under `manage.py runserver`) — never from AppConfig.ready()
directly; see book/startup.py for why.

The commentary cadence is anchored on a `last_commentary_run` timestamp
in system_state rather than on process uptime, so a redeploy or restart
resumes the existing schedule instead of firing a fresh LLM call on every
boot.

Blocked for every `manage.py` subcommand (migrate, seed, shell,
collectstatic, ...) except `runserver` — gunicorn in production and
`manage.py runserver` in dev are the only entry points that should
actually run it.

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
from datetime import date, datetime, timedelta, timezone

from django.conf import settings

from book.db import fetch_one, get_conn, get_state, missing_tables, set_state
from book.ingest import fetch_snapshot
from book.management.commands.compute_attribution import compute_all
from book.management.commands.generate_commentary import generate_all

logger = logging.getLogger(__name__)

REFRESH_SECONDS = settings.REFRESH_SECONDS
COMMENTARY_SECONDS = settings.COMMENTARY_SECONDS

LAST_COMMENTARY_KEY = "last_commentary_run"

# Floor on every loop iteration. Nothing should be able to turn a loop
# into a hot spin — not a clock jump, not a failed state write.
MIN_LOOP_SLEEP = 1.0

_started = False
_start_lock = threading.Lock()

# Latched so an unprovisioned database is reported once, not once per cycle.
_schema_warned = False
_schema_lock = threading.Lock()


def _schema_ready() -> bool:
    """True when schema.sql's tables are all present.

    A database that has only ever been opened is an empty file with no
    tables, and every query against it raises. Rather than let that surface
    as a fresh traceback on each of the two loops every cycle, check first,
    say so once, and idle until bootstrap has run — normally that's
    book.startup at process boot, before this scheduler even starts, but
    this stays as a safety net (a disk that goes missing mid-run, or a
    manual `python manage.py bootstrap` still pending).
    """
    global _schema_warned

    conn = get_conn()
    try:
        missing = missing_tables(conn)
    except Exception:
        logger.exception("scheduler: could not inspect the database schema")
        return False
    finally:
        conn.close()

    with _schema_lock:
        if missing:
            if not _schema_warned:
                logger.error(
                    "scheduler: database has no %s table(s) — skipping cycles until "
                    "`python manage.py bootstrap` has run. This message is logged once.",
                    ", ".join(missing),
                )
                _schema_warned = True
            return False

        if _schema_warned:
            logger.info("scheduler: schema is present again, resuming cycles")
            _schema_warned = False
        return True


def should_start() -> bool:
    """True only for a real server process: gunicorn, or `runserver`'s
    reloaded child. False for every other `manage.py` subcommand and for
    `runserver`'s autoreloader parent, so nothing that runs once per
    process — this scheduler, and the startup bootstrap in book/startup.py
    — fires under `manage.py bootstrap`/`seed`/`shell`/etc., or twice under
    the autoreloader.
    """
    argv = sys.argv
    if not argv or os.path.basename(argv[0]) != "manage.py":
        return True  # not a manage.py invocation (e.g. gunicorn) - always eligible

    if len(argv) < 2 or argv[1] != "runserver":
        return False  # any other subcommand: migrate, seed, shell, collectstatic, ...

    return os.environ.get("RUN_MAIN") == "true"


# --------------------------------------------------------------------
# refresh cadence
# --------------------------------------------------------------------


def _incremental_since(conn) -> str | None:
    """The window to hand compute_all: one day back from the newest
    attribution row, so the most recent pair is always recomputed as
    fresh bars land while settled history is left alone.

    Returns None when pnl_attribution is empty — the first cycle after a
    cold start does a full build.
    """
    row = fetch_one(conn, "SELECT MAX(asof_date) AS latest FROM pnl_attribution")
    latest = row["latest"] if row else None
    if not latest:
        return None
    return (date.fromisoformat(latest) - timedelta(days=1)).isoformat()


def _recompute_attribution() -> None:
    conn = get_conn()
    try:
        since_date = _incremental_since(conn)
        summary = compute_all(conn, since_date=since_date)
    finally:
        conn.close()
    logger.info(
        "scheduler[refresh]: attribution since=%s rows=%d recon_failures=%d",
        summary["since_date"] or "(full rebuild)",
        summary["rows_written"],
        summary["recon_failures"],
    )


def _run_refresh_cycle() -> None:
    if not _schema_ready():
        return

    logger.info("scheduler[refresh]: cycle start")
    try:
        fetch_snapshot()
    except Exception:
        logger.exception("scheduler[refresh]: fetch_snapshot failed")

    try:
        _recompute_attribution()
    except Exception:
        logger.exception("scheduler[refresh]: recompute_attribution failed")

    logger.info("scheduler[refresh]: cycle done, next in %ds", REFRESH_SECONDS)


def _run_refresh_loop() -> None:
    while True:
        _run_refresh_cycle()
        time.sleep(max(MIN_LOOP_SLEEP, REFRESH_SECONDS))


# --------------------------------------------------------------------
# commentary cadence
# --------------------------------------------------------------------


def _generate_commentary() -> bool:
    """Run one commentary pass. Returns True if it actually ran.

    `generate_all` never raises and falls back to rule-based text when the
    model is unavailable, so there is nothing to guard against here.
    """
    summary = generate_all()
    logger.info(
        "scheduler[commentary]: asof=%s action=%s source=%s%s",
        summary["asof_date"],
        summary["action"],
        summary["source"] or "-",
        f" ({summary['error']})" if summary["error"] else "",
    )
    return summary["action"] == "generated"


def _seconds_until_commentary_due(conn) -> float:
    """How long until the next commentary run, from the persisted stamp.

    Zero (due now) when there is no stamp, or when the stamp is corrupt
    or in the future — a bad value should trigger one run that rewrites
    it, not wedge the cadence forever.
    """
    last = get_state(conn, LAST_COMMENTARY_KEY)
    if not last:
        return 0.0
    try:
        last_run = datetime.fromisoformat(last)
    except ValueError:
        logger.warning("scheduler[commentary]: unparseable %s=%r, running now", LAST_COMMENTARY_KEY, last)
        return 0.0

    elapsed = (datetime.now(timezone.utc) - last_run).total_seconds()
    if elapsed < 0:
        logger.warning("scheduler[commentary]: %s is in the future, running now", LAST_COMMENTARY_KEY)
        return 0.0
    return max(0.0, COMMENTARY_SECONDS - elapsed)


def _run_commentary_cycle() -> None:
    """One attempt, then stamp the clock.

    The stamp records the last *attempt*, not the last success, and it is
    written even when commentary is skipped or fails. Otherwise a missing
    module or a failing API would leave the run permanently "due" and the
    loop would spin flat out.
    """
    logger.info("scheduler[commentary]: cycle start")
    try:
        ran = _generate_commentary()
    except Exception:
        logger.exception("scheduler[commentary]: generate_commentary failed")
        ran = False

    conn = get_conn()
    try:
        set_state(conn, LAST_COMMENTARY_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception:
        logger.exception("scheduler[commentary]: could not stamp %s", LAST_COMMENTARY_KEY)
    finally:
        conn.close()

    logger.info(
        "scheduler[commentary]: cycle done (ran=%s), next in %ds", ran, COMMENTARY_SECONDS
    )


def _run_commentary_loop() -> None:
    while True:
        # Checked before reading the cadence stamp, not just before the
        # cycle: `_seconds_until_commentary_due` queries system_state, which
        # is itself one of the tables that may not exist yet. Recheck at the
        # faster cadence while degraded so the loop resumes promptly once
        # bootstrap has run.
        if not _schema_ready():
            time.sleep(max(MIN_LOOP_SLEEP, min(COMMENTARY_SECONDS, REFRESH_SECONDS)))
            continue

        conn = get_conn()
        try:
            wait = _seconds_until_commentary_due(conn)
        except Exception:
            logger.exception("scheduler[commentary]: could not read cadence state")
            wait = COMMENTARY_SECONDS
        finally:
            conn.close()

        if wait <= 0:
            _run_commentary_cycle()
            # Don't re-read the stamp to decide the next sleep: if the
            # write above failed we'd read 0 again and spin.
            wait = COMMENTARY_SECONDS

        time.sleep(max(MIN_LOOP_SLEEP, min(wait, COMMENTARY_SECONDS)))


# --------------------------------------------------------------------


def start() -> None:
    global _started

    if not should_start():
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

    threading.Thread(target=_run_refresh_loop, name="synth-pnl-refresh", daemon=True).start()
    threading.Thread(
        target=_run_commentary_loop, name="synth-pnl-commentary", daemon=True
    ).start()
    logger.info(
        "scheduler: started (refresh every %ds, commentary every %ds)",
        REFRESH_SECONDS,
        COMMENTARY_SECONDS,
    )
