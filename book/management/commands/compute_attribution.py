"""Populate pnl_attribution for every date pair available in the book.

All the maths lives in book/attribution.py (pure, no DB). This module is
only the plumbing: read positions/prices/fx out of SQLite, hand each
consecutive date pair to `attribute()`, write the legs back.
"""

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


def compute_all(conn) -> dict:
    """Recompute pnl_attribution from whatever price/fx history exists.

    Idempotent: every row is upserted on (ticker, asof_date), so running
    it repeatedly converges on the same table. Returns a summary dict.
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
    for position in positions:
        ticker = position["ticker"]
        currency = position["currency"]
        by_date = closes.get(ticker, {})
        fx_by_date = rates.get(currency, {})

        # Only dates where we have both a close and an FX mark are
        # attributable; ISO dates sort chronologically as strings.
        usable = sorted(d for d in by_date if d in fx_by_date)
        skipped_no_fx += len(by_date) - len(usable)

        for prev_date, curr_date in zip(usable, usable[1:]):
            result = attribute(
                shares=position["shares"],
                price_prev=by_date[prev_date],
                price_t=by_date[curr_date],
                fx_prev=fx_by_date[prev_date],
                fx_t=fx_by_date[curr_date],
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
    }


class Command(BaseCommand):
    help = "Compute P&L attribution for all available date pairs. Safe to re-run."

    def handle(self, *args, **options):
        conn = get_conn()
        try:
            summary = compute_all(conn)
        finally:
            conn.close()

        self.stdout.write(f"db: {settings.DB_PATH}")
        self.stdout.write(f"  positions:      {summary['positions']}")
        self.stdout.write(f"  rows written:   {summary['rows_written']}")
        if summary["skipped_no_fx"]:
            self.stdout.write(
                self.style.WARNING(
                    f"  skipped (no fx mark on that date): {summary['skipped_no_fx']}"
                )
            )

        if summary["recon_failures"]:
            self.stdout.write(
                self.style.ERROR(f"  RECON FAILURES: {summary['recon_failures']}")
            )
        else:
            self.stdout.write(self.style.SUCCESS("  recon: all rows tie out"))
