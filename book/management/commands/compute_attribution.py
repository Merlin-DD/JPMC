"""Populate pnl_attribution for every date pair available in the book.

All the maths lives in book/attribution.py (pure, no DB). This module is
only the plumbing: read positions/prices/fx out of SQLite, hand each
consecutive date pair to `attribute()`, write the legs back.
"""

from bisect import bisect_right
from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand

from book.attribution import attribute
from book.db import executemany, fetch_all, get_conn

# One row per (ticker, asof_date): the last bar printed on that market
# date. Live polling writes many intraday bar_ts per asof_date, so the
# close is whichever bar has the greatest bar_ts within the date.
LAST_CLOSE_PER_DATE = """
    SELECT ticker, asof_date, close
    FROM prices p
    WHERE bar_ts = (
        SELECT MAX(bar_ts) FROM prices q
        WHERE q.ticker = p.ticker AND q.asof_date = p.asof_date
    )
    ORDER BY ticker, asof_date
"""

LAST_FX_PER_DATE = """
    SELECT currency, asof_date, usd_per_unit
    FROM fx_rates f
    WHERE bar_ts = (
        SELECT MAX(bar_ts) FROM fx_rates g
        WHERE g.currency = f.currency AND g.asof_date = f.asof_date
    )
    ORDER BY currency, asof_date
"""

UPSERT_ATTRIBUTION = """
    INSERT INTO pnl_attribution (
        ticker, asof_date, equity_delta_pnl, fx_pnl, financing_pnl,
        cross_pnl, total_pnl, recon_diff, recon_ok
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(ticker, asof_date) DO UPDATE SET
        equity_delta_pnl = excluded.equity_delta_pnl,
        fx_pnl = excluded.fx_pnl,
        financing_pnl = excluded.financing_pnl,
        cross_pnl = excluded.cross_pnl,
        total_pnl = excluded.total_pnl,
        recon_diff = excluded.recon_diff,
        recon_ok = excluded.recon_ok
"""


def _day_count(prev_date: str, curr_date: str) -> int:
    """Actual calendar days between two consecutive market dates.

    ACT/360: financing accrues over calendar days, not trading days, so
    the gap across a closed market is charged in full — 1 day within the
    week, 3 across a Fri->Mon weekend, more over a holiday. This is
    deliberate, not an off-by-one.
    """
    return (date.fromisoformat(curr_date) - date.fromisoformat(prev_date)).days


def _window(dates: list[str], since_date: str) -> list[str]:
    """Trim `dates` to those on or after `since_date`, plus the one date
    immediately before it.

    That extra leading date is the anchor: it is the `prev` leg of the
    first pair, and without it the earliest in-window date could not be
    attributed at all. Note it is the preceding *market* date, not
    `since_date - 1 day` — across a weekend or holiday those differ, and
    using the calendar day would silently drop the Fri->Mon pair.

    The net effect: rows written are exactly those with
    ``asof_date >= since_date``.
    """
    for i, day in enumerate(dates):
        if day >= since_date:
            return dates[max(0, i - 1) :]
    return []  # everything predates the window - nothing to recompute


def _asof_fx(fx_dates: list[str], fx_by_date: dict[str, float], market_date: str):
    """The FX mark in force on `market_date`: the most recent one dated
    at or before it. Returns (rate, fx_date), or (None, None).

    An as-of join rather than an exact date match, because FX and equity
    calendars do not line up and never will — different holidays, and a
    24-hour market whose "day" boundary is a convention rather than a
    session. Requiring an exact match silently dropped whole venues out
    of the book whenever the two disagreed by a single day.
    """
    index = bisect_right(fx_dates, market_date) - 1
    if index < 0:
        return None, None
    fx_date = fx_dates[index]
    return fx_by_date[fx_date], fx_date


def compute_all(conn, since_date: str | None = None) -> dict:
    """Recompute pnl_attribution from whatever price/fx history exists.

    With `since_date` (an ISO date string) only pairs whose later date is
    on or after it are recomputed, leaving settled history untouched —
    that is the incremental path the scheduler uses each cycle. With
    `since_date=None` the whole history is rebuilt.

    Idempotent either way: every row is upserted on (ticker, asof_date),
    so running it repeatedly converges on the same table. Returns a
    summary dict.
    """
    positions = fetch_all(
        conn, "SELECT ticker, currency, shares, financing_spread_bps FROM positions"
    )

    closes: dict[str, dict[str, float]] = {}
    for row in fetch_all(conn, LAST_CLOSE_PER_DATE):
        closes.setdefault(row["ticker"], {})[row["asof_date"]] = row["close"]

    rates: dict[str, dict[str, float]] = {}
    for row in fetch_all(conn, LAST_FX_PER_DATE):
        rates.setdefault(row["currency"], {})[row["asof_date"]] = row["usd_per_unit"]

    rows = []
    skipped_no_fx = 0
    max_fx_lag_days = 0
    for position in positions:
        ticker = position["ticker"]
        currency = position["currency"]
        by_date = closes.get(ticker, {})
        fx_by_date = rates.get(currency, {})
        fx_dates = sorted(fx_by_date)

        # Resolve the FX mark in force on each market date up front. A
        # date is attributable when it has both a close and *some* FX
        # mark at or before it; ISO dates sort chronologically as strings.
        usable = []
        fx_for_date: dict[str, float] = {}
        for market_date in sorted(by_date):
            rate, fx_date = _asof_fx(fx_dates, fx_by_date, market_date)
            if rate is None:
                skipped_no_fx += 1
                continue
            usable.append(market_date)
            fx_for_date[market_date] = rate
            max_fx_lag_days = max(max_fx_lag_days, _day_count(fx_date, market_date))

        if since_date is not None:
            usable = _window(usable, since_date)

        for prev_date, curr_date in zip(usable, usable[1:]):
            result = attribute(
                shares=position["shares"],
                price_prev=by_date[prev_date],
                price_t=by_date[curr_date],
                fx_prev=fx_for_date[prev_date],
                fx_t=fx_for_date[curr_date],
                spread_bps=position["financing_spread_bps"],
                days=_day_count(prev_date, curr_date),
            )
            rows.append(
                (
                    ticker,
                    curr_date,
                    result.equity_delta_pnl,
                    result.fx_pnl,
                    result.financing_pnl,
                    result.cross_pnl,
                    result.total_pnl,
                    result.recon_diff,
                    int(result.recon_ok),
                )
            )

    executemany(conn, UPSERT_ATTRIBUTION, rows)

    return {
        "positions": len(positions),
        "rows_written": len(rows),
        "recon_failures": sum(1 for r in rows if not r[8]),
        "skipped_no_fx": skipped_no_fx,
        "max_fx_lag_days": max_fx_lag_days,
        "since_date": since_date,
    }


class Command(BaseCommand):
    help = (
        "Compute P&L attribution. With no arguments this is a full rebuild "
        "over all available date pairs. Safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            dest="since_date",
            default=None,
            metavar="YYYY-MM-DD",
            help=(
                "Only recompute rows with asof_date on or after this date. "
                "Omit for a full rebuild."
            ),
        )

    def handle(self, *args, **options):
        since_date = options["since_date"]
        if since_date is not None:
            # Fail loudly on a malformed date rather than silently
            # string-comparing garbage against ISO dates.
            date.fromisoformat(since_date)

        conn = get_conn()
        try:
            summary = compute_all(conn, since_date=since_date)
        finally:
            conn.close()

        self.stdout.write(f"db: {settings.DB_PATH}")
        self.stdout.write(
            f"  mode:           {'full rebuild' if since_date is None else f'incremental since {since_date}'}"
        )
        self.stdout.write(f"  positions:      {summary['positions']}")
        self.stdout.write(f"  rows written:   {summary['rows_written']}")
        if summary["skipped_no_fx"]:
            self.stdout.write(
                self.style.WARNING(
                    f"  skipped (no fx mark at or before that date): {summary['skipped_no_fx']}"
                )
            )
        if summary["max_fx_lag_days"]:
            message = (
                f"  worst fx lag:   {summary['max_fx_lag_days']} day(s) "
                "between a market date and the fx mark used for it"
            )
            # A weekend gap is normal; a week apart means fx ingestion is
            # lagging price ingestion and the marks are worth a look.
            if summary["max_fx_lag_days"] > 4:
                self.stdout.write(self.style.WARNING(message))
            else:
                self.stdout.write(message)

        if summary["recon_failures"]:
            self.stdout.write(
                self.style.ERROR(f"  RECON FAILURES: {summary['recon_failures']}")
            )
        else:
            self.stdout.write(self.style.SUCCESS("  recon: all rows tie out"))
