"""Fetch seed market data for the synth_pnl demo book.

Run locally only — never on Render. Writes data/positions.csv,
data/prices.csv, and data/fx.csv. These are committed as a seed
fallback so a cold start always renders something even without
network access to yfinance.

Each price/fx row gets bar_ts (the trading day's actual venue-local
market-close instant, tz-aware UTC), fetched_at (when this script ran),
and asof_date (bar_ts converted back to the venue's local date) — the
same three-timestamp shape book/ingest.py writes for live snapshots,
just at daily instead of per-minute granularity.

yfinance's daily download can include a same-day/weekend row that isn't
a real completed session (e.g. a cached quote stamped with today's
date). Two hard filters guard against ever emitting a bad bar_ts for
that: weekend dates are dropped outright (no venue in this book trades
Sat/Sun), and any date whose venue session hasn't actually closed yet
as of "now" is dropped too — a bar_ts must never be later than now.

Usage:
    python scripts/refresh_prices.py
"""

import random
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# ticker, name, currency, sector
POSITIONS = [
    ("AAPL", "Apple Inc", "USD", "Technology"),
    ("MSFT", "Microsoft Corp", "USD", "Technology"),
    ("0700.HK", "Tencent Holdings", "HKD", "Communication Services"),
    ("0005.HK", "HSBC Holdings", "HKD", "Financials"),
    ("7203.T", "Toyota Motor Corp", "JPY", "Consumer Discretionary"),
    ("6758.T", "Sony Group Corp", "JPY", "Technology"),
    ("SAP.DE", "SAP SE", "EUR", "Technology"),
    ("SIE.DE", "Siemens AG", "EUR", "Industrials"),
]

FX_CROSSES = {
    "HKD": "HKDUSD=X",
    "JPY": "JPYUSD=X",
    "EUR": "EURUSD=X",
}

# Each currency's home venue, and that venue's local close time.
CURRENCY_VENUE = {
    "USD": "NYSE",
    "HKD": "HKEX",
    "JPY": "TSE",
    "EUR": "XETRA",
}

VENUE_CLOSE = {
    "NYSE": (ZoneInfo("America/New_York"), dt_time(16, 0)),
    "XETRA": (ZoneInfo("Europe/Berlin"), dt_time(17, 30)),
    "HKEX": (ZoneInfo("Asia/Hong_Kong"), dt_time(16, 0)),
    "TSE": (ZoneInfo("Asia/Tokyo"), dt_time(15, 0)),
}

# For daily bars, "stale" means older than the last completed session
# should be, not older than 5 minutes (that threshold is for the live
# 1-minute-interval poller in book/ingest.py). One day is a coarse but
# simple proxy for "at least one more session has likely closed since".
STALE_THRESHOLD_DAILY = timedelta(hours=24)

RANDOM_SEED = 42

PRICE_COLUMNS = ["ticker", "bar_ts", "fetched_at", "asof_date", "close", "is_stale"]
FX_COLUMNS = ["currency", "bar_ts", "fetched_at", "asof_date", "usd_per_unit", "is_stale"]


def build_positions_frame() -> pd.DataFrame:
    rng = random.Random(RANDOM_SEED)
    rows = []
    for ticker, name, currency, sector in POSITIONS:
        rows.append(
            {
                "ticker": ticker,
                "name": name,
                "currency": currency,
                "shares": rng.randint(50, 2000),
                "financing_spread_bps": rng.randint(30, 120),
                "sector": sector,
            }
        )
    return pd.DataFrame(rows)


def fetch_market_data(symbols: list[str]) -> pd.DataFrame:
    return yf.download(
        symbols,
        period="1mo",
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
        progress=False,
    )


def compute_full_index(raw: pd.DataFrame, symbols: list[str]) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex([])
    for symbol in symbols:
        closes = raw[symbol]["Close"].dropna().copy()
        closes.index = closes.index.tz_localize(None).normalize()
        index = index.union(closes.index)
    return index.sort_values()


def _valid_sessions(
    dates: pd.DatetimeIndex, venue: str, now_utc: datetime
) -> list[tuple[pd.Timestamp, datetime]]:
    """(date, bar_ts_utc) pairs for weekday dates whose venue session has
    actually closed by now_utc. Weekends and not-yet-closed sessions are
    dropped entirely, never fabricated."""
    tz, close_time = VENUE_CLOSE[venue]
    sessions = []
    for date in dates:
        if date.weekday() >= 5:
            continue
        bar_ts_utc = datetime.combine(date.date(), close_time, tzinfo=tz).astimezone(timezone.utc)
        if bar_ts_utc > now_utc:
            continue
        sessions.append((date, bar_ts_utc))
    return sessions


def _rows_for_series(
    label: str,
    closes: pd.Series,
    venue: str,
    full_index: pd.DatetimeIndex,
    fetched_at: str,
    now_utc: datetime,
) -> list[tuple]:
    tz, _ = VENUE_CLOSE[venue]
    rows = []
    for date, bar_ts_utc in _valid_sessions(full_index, venue, now_utc):
        if date not in closes.index or pd.isna(closes.loc[date]):
            continue
        asof_date = bar_ts_utc.astimezone(tz).date().isoformat()
        is_stale = int((now_utc - bar_ts_utc) > STALE_THRESHOLD_DAILY)
        rows.append(
            (
                label,
                bar_ts_utc.isoformat(timespec="seconds"),
                fetched_at,
                asof_date,
                float(closes.loc[date]),
                is_stale,
            )
        )
    return rows


def build_prices_frame(
    raw: pd.DataFrame,
    equity_tickers: list[str],
    ticker_currency: dict[str, str],
    full_index: pd.DatetimeIndex,
    fetched_at: str,
    now_utc: datetime,
) -> pd.DataFrame:
    rows = []
    for ticker in equity_tickers:
        venue = CURRENCY_VENUE[ticker_currency[ticker]]
        closes = raw[ticker]["Close"].copy()
        closes.index = closes.index.tz_localize(None).normalize()
        rows.extend(_rows_for_series(ticker, closes, venue, full_index, fetched_at, now_utc))
    return pd.DataFrame(rows, columns=PRICE_COLUMNS)


def build_fx_frame(
    raw: pd.DataFrame, full_index: pd.DatetimeIndex, fetched_at: str, now_utc: datetime
) -> pd.DataFrame:
    rows = []
    for currency, fx_ticker in FX_CROSSES.items():
        venue = CURRENCY_VENUE[currency]
        closes = raw[fx_ticker]["Close"].copy()
        closes.index = closes.index.tz_localize(None).normalize()
        rows.extend(_rows_for_series(currency, closes, venue, full_index, fetched_at, now_utc))

    # USD is the book's base currency — every position/report needs a USD
    # row even though there's no USDUSD=X cross to fetch. Pegged at 1.0
    # for every valid NYSE session date, subject to the same staleness
    # rule as everything else.
    venue = CURRENCY_VENUE["USD"]
    tz, _ = VENUE_CLOSE[venue]
    for date, bar_ts_utc in _valid_sessions(full_index, venue, now_utc):
        asof_date = bar_ts_utc.astimezone(tz).date().isoformat()
        is_stale = int((now_utc - bar_ts_utc) > STALE_THRESHOLD_DAILY)
        rows.append(
            ("USD", bar_ts_utc.isoformat(timespec="seconds"), fetched_at, asof_date, 1.0, is_stale)
        )

    return pd.DataFrame(rows, columns=FX_COLUMNS)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    equity_tickers = [p[0] for p in POSITIONS]
    ticker_currency = {ticker: currency for ticker, _, currency, _ in POSITIONS}
    fx_tickers = list(FX_CROSSES.values())
    raw = fetch_market_data(equity_tickers + fx_tickers)

    now_utc = datetime.now(timezone.utc)
    fetched_at = now_utc.isoformat(timespec="seconds")
    full_index = compute_full_index(raw, equity_tickers + fx_tickers)

    positions = build_positions_frame()
    prices = build_prices_frame(raw, equity_tickers, ticker_currency, full_index, fetched_at, now_utc)
    fx = build_fx_frame(raw, full_index, fetched_at, now_utc)

    positions.to_csv(DATA_DIR / "positions.csv", index=False)
    prices.to_csv(DATA_DIR / "prices.csv", index=False)
    fx.to_csv(DATA_DIR / "fx.csv", index=False)

    print(
        f"wrote {len(positions)} positions, {len(prices)} price rows, "
        f"{len(fx)} fx rows to {DATA_DIR}"
    )
    if not prices.empty:
        print(f"prices bar_ts range: {prices['bar_ts'].min()} .. {prices['bar_ts'].max()}")


if __name__ == "__main__":
    main()
