"""Read model for /risk: exposure, concentration, volatility.

Stdlib only for the maths — `statistics` and `math`, not pandas. This
runs on the request path, and pandas costs more to import than the whole
calculation costs to run.

Same accounting conventions as the dashboard: USD everywhere, negatives
in parentheses, colour never the sole carrier of sign.
"""

import math
from statistics import stdev

from book.dashboard import format_usd, sign_class
from book.db import fetch_all
from book.marks import asof_rate, load_closes, load_rates

# Trading days per year. The conventional equity figure; financing
# accrues ACT/360 but volatility scales on trading observations.
TRADING_DAYS = 252

# How many market dates the stacked-bar chart covers.
CHART_DAYS = 30

# Order matters: this is the stacking order in the chart, the column
# order in tables, and the CSS custom property suffix for each series.
LEGS = (
    ("equity_delta_pnl", "Equity Delta", "equity"),
    ("fx_pnl", "FX", "fx"),
    ("financing_pnl", "Financing", "financing"),
    ("cross_pnl", "Cross", "cross"),
)

POSITIONS = """
    SELECT ticker, name, currency, sector, shares, financing_spread_bps
    FROM positions
    ORDER BY ticker
"""

DAILY_LEGS = """
    SELECT asof_date,
           SUM(equity_delta_pnl) AS equity_delta_pnl,
           SUM(fx_pnl)           AS fx_pnl,
           SUM(financing_pnl)    AS financing_pnl,
           SUM(cross_pnl)        AS cross_pnl,
           SUM(total_pnl)        AS total_pnl,
           COUNT(*)              AS n_positions
    FROM pnl_attribution
    GROUP BY asof_date
    ORDER BY asof_date
"""


def format_pct(value, places: int = 2) -> str:
    """Percentage, negatives in parentheses like every other number here."""
    if value is None:
        return "—"
    pct = value * 100
    if abs(pct) < 0.005:
        return "0.00%"
    if pct < 0:
        return f"({abs(pct):,.{places}f}%)"
    return f"{pct:,.{places}f}%"


def _money(value) -> dict:
    return {"value": value, "display": format_usd(value), "sign": sign_class(value)}


def _exposures(conn):
    """Current USD market value per position, at each position's own
    latest mark. Returns (rows, gross, net).

    Gross sums absolute values, net sums signed ones: for an all-long
    book the two are equal, and they diverge the moment a short appears.
    Reporting both makes that visible rather than implied.
    """
    positions = fetch_all(conn, POSITIONS)
    closes = load_closes(conn)
    rates = load_rates(conn)

    rows = []
    gross = 0.0
    net = 0.0
    for position in positions:
        by_date = closes.get(position["ticker"], {})
        if not by_date:
            continue
        asof_date = max(by_date)
        price = by_date[asof_date]

        fx_by_date = rates.get(position["currency"], {})
        rate, _fx_date = asof_rate(sorted(fx_by_date), fx_by_date, asof_date)
        if rate is None:
            continue

        market_value = position["shares"] * price * rate
        gross += abs(market_value)
        net += market_value
        rows.append(
            {
                "ticker": position["ticker"],
                "name": position["name"],
                "currency": position["currency"],
                "sector": position["sector"],
                "shares": position["shares"],
                "asof_date": asof_date,
                "market_value": market_value,
            }
        )

    return rows, gross, net


def _group(rows, key: str, gross: float):
    """Gross/net exposure bucketed by currency or sector."""
    buckets: dict[str, dict] = {}
    for row in rows:
        bucket = buckets.setdefault(row[key], {"gross": 0.0, "net": 0.0, "count": 0})
        bucket["gross"] += abs(row["market_value"])
        bucket["net"] += row["market_value"]
        bucket["count"] += 1

    out = []
    for name in sorted(buckets, key=lambda k: -buckets[k]["gross"]):
        bucket = buckets[name]
        share = bucket["gross"] / gross if gross else None
        out.append(
            {
                "name": name,
                "count": bucket["count"],
                "gross": _money(bucket["gross"]),
                "net": _money(bucket["net"]),
                "share": share,
                "share_display": format_pct(share),
            }
        )
    return out


def _concentration(rows, gross: float):
    ranked = sorted(rows, key=lambda r: -abs(r["market_value"]))
    if not ranked or not gross:
        return {"largest": None, "top3": None, "ranked": []}

    largest_share = abs(ranked[0]["market_value"]) / gross
    top3_share = sum(abs(r["market_value"]) for r in ranked[:3]) / gross

    return {
        "largest_ticker": ranked[0]["ticker"],
        "largest_share": largest_share,
        "largest_share_display": format_pct(largest_share),
        "top3_tickers": [r["ticker"] for r in ranked[:3]],
        "top3_share": top3_share,
        "top3_share_display": format_pct(top3_share),
        "ranked": [
            {
                "ticker": r["ticker"],
                "name": r["name"],
                "currency": r["currency"],
                "sector": r["sector"],
                "exposure": _money(r["market_value"]),
                "share": abs(r["market_value"]) / gross,
                "share_display": format_pct(abs(r["market_value"]) / gross),
            }
            for r in ranked
        ],
    }


def _daily_gross(conn, position_count: int):
    """Gross USD exposure on each market date, for turning daily P&L into
    a return. Only dates carrying every position are returned — a day
    missing six of eight names is not a portfolio observation."""
    positions = fetch_all(conn, POSITIONS)
    closes = load_closes(conn)
    rates = load_rates(conn)

    fx_dates = {c: sorted(d) for c, d in rates.items()}
    per_date: dict[str, float] = {}
    counts: dict[str, int] = {}

    for position in positions:
        by_date = closes.get(position["ticker"], {})
        currency = position["currency"]
        fx_by_date = rates.get(currency, {})
        for asof_date, price in by_date.items():
            rate, _ = asof_rate(fx_dates.get(currency, []), fx_by_date, asof_date)
            if rate is None:
                continue
            per_date[asof_date] = per_date.get(asof_date, 0.0) + abs(
                position["shares"] * price * rate
            )
            counts[asof_date] = counts.get(asof_date, 0) + 1

    return {d: v for d, v in per_date.items() if counts.get(d) == position_count}


def _volatility(daily, gross_by_date, position_count: int):
    """Annualized volatility of daily book returns.

    A return needs a denominator, so each day's P&L is divided by the
    book's gross exposure at the *previous* mark — P&L alone is a
    currency amount, not a return, and its standard deviation would scale
    with book size rather than describe risk.

    Days where some venue was shut are excluded outright. Averaging a
    two-venue day into a portfolio series understates dispersion, and
    this book has three such dates from venue holidays.
    """
    complete = [d for d in daily if d["n_positions"] == position_count]
    excluded = len(daily) - len(complete)

    returns = []
    for previous, current in zip(complete, complete[1:]):
        denominator = gross_by_date.get(previous["asof_date"])
        if not denominator:
            continue
        returns.append(current["total_pnl"] / denominator)

    if len(returns) < 2:
        return {
            "observations": len(returns),
            "excluded_days": excluded,
            "insufficient": True,
            "daily": None,
            "annualized": None,
            "daily_display": "—",
            "annualized_display": "—",
            "pnl_stdev_display": "—",
        }

    daily_vol = stdev(returns)
    pnl_values = [
        c["total_pnl"] for p, c in zip(complete, complete[1:])
    ]

    return {
        "observations": len(returns),
        "excluded_days": excluded,
        "insufficient": False,
        "daily": daily_vol,
        "annualized": daily_vol * math.sqrt(TRADING_DAYS),
        "daily_display": format_pct(daily_vol),
        "annualized_display": format_pct(daily_vol * math.sqrt(TRADING_DAYS)),
        "pnl_stdev": stdev(pnl_values),
        "pnl_stdev_display": format_usd(stdev(pnl_values)),
    }


def _chart(daily):
    """Last CHART_DAYS market dates of the four legs, stacked.

    Values ship alongside pre-formatted `displays` so the chart tooltip
    uses the same accounting format as every table on the site.
    """
    window = daily[-CHART_DAYS:]
    return {
        "labels": [d["asof_date"] for d in window],
        "series": [
            {
                "key": key,
                "label": label,
                "values": [d[column] for d in window],
                "displays": [format_usd(d[column]) for d in window],
            }
            for column, label, key in LEGS
        ],
        "totals": [format_usd(d["total_pnl"]) for d in window],
        "days": len(window),
    }


def build_risk(conn) -> dict:
    """Everything /risk renders."""
    exposure_rows, gross, net = _exposures(conn)
    position_count = len(exposure_rows)

    daily = [dict(row) for row in fetch_all(conn, DAILY_LEGS)]
    gross_by_date = _daily_gross(conn, position_count)

    return {
        "empty": not exposure_rows or not daily,
        "position_count": position_count,
        "asof_dates": sorted({r["asof_date"] for r in exposure_rows}, reverse=True),
        "gross": _money(gross),
        "net": _money(net),
        "long_short_equal": abs(gross - net) < 0.005,
        "by_currency": _group(exposure_rows, "currency", gross),
        "by_sector": _group(exposure_rows, "sector", gross),
        "concentration": _concentration(exposure_rows, gross),
        "volatility": _volatility(daily, gross_by_date, position_count),
        "chart": _chart(daily),
        "trading_days": TRADING_DAYS,
    }
