"""Resolving price and FX marks by market date.

Shared by the attribution command and the risk page so the two can never
disagree about which rate valued which position — a class of bug this
book has already been bitten by once.

Stdlib plus book.db only: no pandas, no yfinance. The risk page runs on
the request path.
"""

from bisect import bisect_right

from book.db import fetch_all

# One row per (ticker, market date): the last bar printed that day. Live
# polling writes many intraday bar_ts per asof_date, so the close is
# whichever bar has the greatest bar_ts within the date.
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


def load_closes(conn) -> dict[str, dict[str, float]]:
    """{ticker: {asof_date: close}}"""
    closes: dict[str, dict[str, float]] = {}
    for row in fetch_all(conn, LAST_CLOSE_PER_DATE):
        closes.setdefault(row["ticker"], {})[row["asof_date"]] = row["close"]
    return closes


def load_rates(conn) -> dict[str, dict[str, float]]:
    """{currency: {asof_date: usd_per_unit}}"""
    rates: dict[str, dict[str, float]] = {}
    for row in fetch_all(conn, LAST_FX_PER_DATE):
        rates.setdefault(row["currency"], {})[row["asof_date"]] = row["usd_per_unit"]
    return rates


def asof_rate(fx_dates: list[str], fx_by_date: dict[str, float], market_date: str):
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
