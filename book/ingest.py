"""Live market data ingestion for the synth_pnl book.

fetch_snapshot() is the entry point: one batched yfinance call per cycle,
covering every position ticker and every FX cross the book needs.

Every price/fx row stores two timestamps: `bar_ts` (when the underlying
market bar actually printed, tz-aware UTC) and `fetched_at` (when we
polled). A symbol appearing in the yfinance response does not mean its
bar is fresh — a closed venue still returns its last printed bar, so
staleness is judged purely by `fetched_at - bar_ts`, the same way for
bars pulled fresh this cycle and for bars carried forward because a
symbol was missing from the response entirely.
"""

import logging
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

from book.db import executemany, fetch_all, fetch_one, get_conn, set_state

logger = logging.getLogger(__name__)

FX_CROSSES = {
    "HKD": "HKDUSD=X",
    "JPY": "JPYUSD=X",
    "EUR": "EURUSD=X",
}

# Each currency's home venue, used only to derive asof_date (the market's
# local calendar date) from a UTC bar_ts. Approximate by design: e.g. a
# USD position may actually trade on NASDAQ rather than NYSE, but both
# share America/New_York, so the date derivation is correct either way.
CURRENCY_VENUE = {
    "USD": "NYSE",
    "HKD": "HKEX",
    "JPY": "TSE",
    "EUR": "XETRA",
}

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2  # attempt 1 -> wait 2s -> attempt 2 -> wait 4s -> attempt 3

STALE_THRESHOLD = timedelta(minutes=5)

VENUES = {
    "NYSE": {"tz": ZoneInfo("America/New_York"), "open": dt_time(9, 30), "close": dt_time(16, 0)},
    "XETRA": {"tz": ZoneInfo("Europe/Berlin"), "open": dt_time(9, 0), "close": dt_time(17, 30)},
    "HKEX": {"tz": ZoneInfo("Asia/Hong_Kong"), "open": dt_time(9, 30), "close": dt_time(16, 0)},
    "TSE": {"tz": ZoneInfo("Asia/Tokyo"), "open": dt_time(9, 0), "close": dt_time(15, 0)},
}


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _venue_tz(currency: str):
    venue = CURRENCY_VENUE.get(currency)
    if venue is None:
        logger.warning("no venue mapped for currency %s, using UTC for asof_date", currency)
        return timezone.utc
    return VENUES[venue]["tz"]


def is_future_bar(bar_ts: datetime, now: datetime | None = None) -> bool:
    """True if bar_ts is later than now. A future-dated market bar is
    always a bug — a bad synthetic timestamp, clock skew, or corrupt
    data — never valid data. Used as a hard guard both here and in the
    seed loader before any such row is written."""
    now = now or datetime.now(timezone.utc)
    return bar_ts > now


def _download_with_retry(symbols):
    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            data = yf.download(
                symbols,
                period="5d",
                interval="1m",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
            )
            if data is None or data.empty:
                raise ValueError("yfinance returned an empty frame")
            return data
        except Exception as exc:  # noqa: BLE001 - any failure is retried, then re-raised
            last_exc = exc
            logger.warning("fetch attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, exc)
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    raise last_exc


def _last_bar(raw, symbol):
    """Return (close, bar_ts) for a symbol's last non-null bar this cycle,
    bar_ts normalized to tz-aware UTC, or None if the symbol has no data
    at all in the response.

    yfinance's intraday index is normally tz-aware in the exchange's
    local timezone; the naive-timestamp branch below is defensive in
    case a particular symbol/version returns naive timestamps instead.
    """
    try:
        closes = raw[symbol]["Close"].dropna()
    except KeyError:
        return None
    if closes.empty:
        return None
    bar_ts = closes.index[-1]
    if bar_ts.tzinfo is None:
        bar_ts = bar_ts.tz_localize(timezone.utc)
    else:
        bar_ts = bar_ts.tz_convert(timezone.utc)
    return float(closes.iloc[-1]), bar_ts.to_pydatetime()


def _write_prices(conn, positions, raw, fetched_at):
    rows = []
    for ticker, currency in positions:
        bar = _last_bar(raw, ticker)
        if bar is not None:
            close, bar_ts = bar
        else:
            last = fetch_one(
                conn,
                "SELECT close, bar_ts FROM prices WHERE ticker = ? ORDER BY bar_ts DESC LIMIT 1",
                (ticker,),
            )
            if last is None:
                logger.warning("no fresh or stored price for %s, skipping this cycle", ticker)
                continue
            close = last["close"]
            bar_ts = datetime.fromisoformat(last["bar_ts"])

        if is_future_bar(bar_ts, fetched_at):
            logger.warning(
                "rejecting future-dated bar for %s: bar_ts=%s > now=%s", ticker, bar_ts, fetched_at
            )
            continue

        asof_date = bar_ts.astimezone(_venue_tz(currency)).date().isoformat()
        is_stale = int((fetched_at - bar_ts) > STALE_THRESHOLD)
        rows.append((ticker, _iso(bar_ts), _iso(fetched_at), asof_date, close, is_stale))

    executemany(
        conn,
        """
        INSERT INTO prices (ticker, bar_ts, fetched_at, asof_date, close, is_stale)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, bar_ts) DO UPDATE SET
            fetched_at = excluded.fetched_at,
            asof_date = excluded.asof_date,
            close = excluded.close,
            is_stale = excluded.is_stale
        """,
        rows,
    )
    return len(rows)


def _write_fx(conn, currencies, raw, fetched_at):
    # USD is the book's base currency, pegged at 1.0 — there's no real
    # market bar for it, so it's always "fresh" as of the poll itself.
    usd_asof_date = fetched_at.astimezone(_venue_tz("USD")).date().isoformat()
    rows = [("USD", _iso(fetched_at), _iso(fetched_at), usd_asof_date, 1.0, 0)]

    for currency in currencies:
        fx_symbol = FX_CROSSES.get(currency)
        if fx_symbol is None:
            logger.warning("no FX cross configured for currency %s", currency)
            continue

        bar = _last_bar(raw, fx_symbol)
        if bar is not None:
            rate, bar_ts = bar
        else:
            last = fetch_one(
                conn,
                "SELECT usd_per_unit, bar_ts FROM fx_rates WHERE currency = ? ORDER BY bar_ts DESC LIMIT 1",
                (currency,),
            )
            if last is None:
                logger.warning("no fresh or stored fx rate for %s, skipping this cycle", currency)
                continue
            rate = last["usd_per_unit"]
            bar_ts = datetime.fromisoformat(last["bar_ts"])

        if is_future_bar(bar_ts, fetched_at):
            logger.warning(
                "rejecting future-dated fx bar for %s: bar_ts=%s > now=%s", currency, bar_ts, fetched_at
            )
            continue

        asof_date = bar_ts.astimezone(_venue_tz(currency)).date().isoformat()
        is_stale = int((fetched_at - bar_ts) > STALE_THRESHOLD)
        rows.append((currency, _iso(bar_ts), _iso(fetched_at), asof_date, rate, is_stale))

    executemany(
        conn,
        """
        INSERT INTO fx_rates (currency, bar_ts, fetched_at, asof_date, usd_per_unit, is_stale)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(currency, bar_ts) DO UPDATE SET
            fetched_at = excluded.fetched_at,
            asof_date = excluded.asof_date,
            usd_per_unit = excluded.usd_per_unit,
            is_stale = excluded.is_stale
        """,
        rows,
    )
    return len(rows)


class _FetchFailed(Exception):
    """Raised for the known/expected fetch_snapshot failure modes, so the
    message text can distinguish them from unexpected bugs."""


def _run_fetch_cycle(conn, fetched_at) -> None:
    positions = [
        (row["ticker"], row["currency"])
        for row in fetch_all(conn, "SELECT ticker, currency FROM positions")
    ]
    if not positions:
        raise _FetchFailed("no positions in book, nothing to fetch")

    tickers = [ticker for ticker, _ in positions]
    currencies = sorted({currency for _, currency in positions if currency != "USD"})
    fx_symbols = [FX_CROSSES[c] for c in currencies if c in FX_CROSSES]

    try:
        raw = _download_with_retry(tickers + fx_symbols)
    except Exception as exc:
        raise _FetchFailed(f"download failed after {MAX_ATTEMPTS} attempts: {exc}") from exc

    # Rows entirely outside every venue's session (e.g. deep night hours
    # with no trading anywhere) contribute nothing; drop them before
    # picking each ticker's last non-null close.
    raw = raw.dropna(how="all")

    price_rows_written = _write_prices(conn, positions, raw, fetched_at)
    _write_fx(conn, currencies, raw, fetched_at)

    if price_rows_written == 0:
        raise _FetchFailed(
            "download succeeded but yielded zero usable price rows after NaN-dropping "
            "(no fresh bar and no stored fallback for any position)"
        )


def fetch_snapshot() -> None:
    """Pull one live snapshot for every position and FX cross in the book.

    On any non-success path — no positions to fetch, the download failing
    after retries, the download succeeding but producing zero usable price
    rows, or any other unexpected error — existing prices/fx rows are left
    untouched, nothing is written, and system_state.last_error is always
    set to a non-empty, distinguishing message (also logged to the
    console). A failing ingest must never blank the dashboard, and it must
    never fail silently either.
    """
    conn = get_conn()
    try:
        fetched_at = datetime.now(timezone.utc)
        attempt_iso = _iso(fetched_at)
        set_state(conn, "last_attempt", attempt_iso)

        try:
            _run_fetch_cycle(conn, fetched_at)
        except _FetchFailed as exc:
            message = f"{attempt_iso}: {exc}"
            logger.error("fetch_snapshot: %s", message)
            set_state(conn, "last_error", message)
            return
        except Exception as exc:  # noqa: BLE001 - must still be recorded, not just logged
            message = f"{attempt_iso}: unexpected error: {exc}"
            logger.exception("fetch_snapshot: %s", message)
            set_state(conn, "last_error", message)
            return

        set_state(conn, "last_successful_fetch", attempt_iso)
        set_state(conn, "last_error", "")
    finally:
        conn.close()


def market_status(now: datetime | None = None) -> dict:
    """Open/closed per venue, derived only from the current UTC time and
    each venue's nominal Mon-Fri session window.

    Limitation: this ignores exchange holidays entirely (and intraday
    lunch breaks on HKEX/TSE) — a venue will report "open" on a holiday
    if the wall-clock time falls inside its normal session. Treat this as
    a rough liveness signal for the scheduler/UI, not an authoritative
    trading calendar.
    """
    now = now or datetime.now(timezone.utc)
    status = {}
    for venue, cfg in VENUES.items():
        local = now.astimezone(cfg["tz"])
        is_weekday = local.weekday() < 5
        in_session = cfg["open"] <= local.time() < cfg["close"]
        status[venue] = "open" if (is_weekday and in_session) else "closed"
    return status
