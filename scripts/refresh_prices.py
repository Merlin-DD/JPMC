"""Fetch seed market data for the synth_pnl demo book.

Run locally only — never on Render. Writes data/positions.csv,
data/prices.csv, and data/fx.csv. These are committed as a seed
fallback so a cold start always renders something even without
network access to yfinance.

Usage:
    python scripts/refresh_prices.py
"""

import random
from pathlib import Path

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

RANDOM_SEED = 42


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


def build_prices_frame(
    raw: pd.DataFrame, equity_tickers: list[str], full_index: pd.DatetimeIndex
) -> pd.DataFrame:
    rows = []
    for ticker in equity_tickers:
        closes = raw[ticker]["Close"].copy()
        closes.index = closes.index.tz_localize(None).normalize()
        series = closes.reindex(full_index)
        is_stale = series.isna().astype(int)
        series = series.ffill()
        rows.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "ts": full_index.strftime("%Y-%m-%d"),
                    "close": series.to_numpy(),
                    "is_stale": is_stale.to_numpy(),
                }
            )
        )
    result = pd.concat(rows, ignore_index=True)
    return result.dropna(subset=["close"])


def build_fx_frame(raw: pd.DataFrame, full_index: pd.DatetimeIndex) -> pd.DataFrame:
    rows = []
    for currency, fx_ticker in FX_CROSSES.items():
        closes = raw[fx_ticker]["Close"].copy()
        closes.index = closes.index.tz_localize(None).normalize()
        series = closes.reindex(full_index)
        is_stale = series.isna().astype(int)
        series = series.ffill()
        rows.append(
            pd.DataFrame(
                {
                    "currency": currency,
                    "ts": full_index.strftime("%Y-%m-%d"),
                    "usd_per_unit": series.to_numpy(),
                    "is_stale": is_stale.to_numpy(),
                }
            )
        )

    # USD is the book's base currency — every position/report needs a
    # USD row even though there's no USDUSD=X cross to fetch.
    rows.append(
        pd.DataFrame(
            {
                "currency": "USD",
                "ts": full_index.strftime("%Y-%m-%d"),
                "usd_per_unit": 1.0,
                "is_stale": 0,
            }
        )
    )

    result = pd.concat(rows, ignore_index=True)
    return result.dropna(subset=["usd_per_unit"])


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    equity_tickers = [p[0] for p in POSITIONS]
    fx_tickers = list(FX_CROSSES.values())
    raw = fetch_market_data(equity_tickers + fx_tickers)

    full_index = compute_full_index(raw, equity_tickers + fx_tickers)
    positions = build_positions_frame()
    prices = build_prices_frame(raw, equity_tickers, full_index)
    fx = build_fx_frame(raw, full_index)

    positions.to_csv(DATA_DIR / "positions.csv", index=False)
    prices.to_csv(DATA_DIR / "prices.csv", index=False)
    fx.to_csv(DATA_DIR / "fx.csv", index=False)

    print(
        f"wrote {len(positions)} positions, {len(prices)} price rows, "
        f"{len(fx)} fx rows to {DATA_DIR}"
    )


if __name__ == "__main__":
    main()
