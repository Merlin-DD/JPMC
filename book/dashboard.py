"""Read model for the dashboard.

One `build_summary()` call assembles everything both `/` (server-rendered)
and `/api/summary` (JSON poll) need, so the two can never drift apart.

Every number is emitted twice: the raw float under `<name>`, and a
presentation string under `<name>_display`. Formatting lives here rather
than in the template or the JS so the first paint and every subsequent
poll are formatted by the same code.

Accounting convention, applied everywhere without exception: negatives
render in parentheses — (36.21), never -36.21. Colour is layered on top
as reinforcement only, never as the sole carrier of sign.

Two things this module is careful about:

*Positions do not share an asof_date.* `asof_date` is the market date in
the venue's own timezone, so on any given run XETRA names can be a day
ahead of TSE names. There is no single book-wide "latest date" — each
ticker's newest row is taken independently, and the spread of dates is
reported rather than papered over.

*Staleness is a property of the marks, not of the poller.* A successful
fetch 30 seconds ago that returned Friday's closes is not live data. Age
is measured from the newest `bar_ts`, and only from `prices`: the
`fx_rates` table carries a synthetic USD row stamped at fetch time, which
would make every book look permanently fresh.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from book.db import fetch_all, fetch_one, get_state
from book.venues import CURRENCY_VENUE, market_status

HKT = ZoneInfo("Asia/Hong_Kong")

# Display order for the venue pills.
VENUE_ORDER = ("HKEX", "NYSE", "TSE", "XETRA")

# (column, card label) for the four legs, in the order they appear in the
# KPI strip and as table columns.
LEGS = (
    ("equity_delta_pnl", "Equity Delta"),
    ("fx_pnl", "FX"),
    ("financing_pnl", "Financing"),
    ("cross_pnl", "Cross"),
)

# A cycle is fetch-time *plus* the sleep, so with REFRESH_SECONDS=60 the
# real period is ~105s. Thresholds are multiples of the configured
# interval with enough headroom that ordinary jitter never trips amber.
WARN_MULTIPLE = 5
STALE_MULTIPLE = 15

# Each ticker's most recent attribution row, independently of every other
# ticker's — see the module docstring on why a single global MAX(asof_date)
# silently drops whole venues.
LATEST_ATTRIBUTION_PER_TICKER = """
    SELECT a.ticker, a.asof_date, p.name, p.currency, p.shares,
           a.equity_delta_pnl, a.fx_pnl, a.financing_pnl, a.cross_pnl,
           a.total_pnl, a.recon_diff, a.recon_ok
    FROM pnl_attribution a
    JOIN positions p ON p.ticker = a.ticker
    WHERE a.asof_date = (
        SELECT MAX(b.asof_date) FROM pnl_attribution b WHERE b.ticker = a.ticker
    )
    ORDER BY a.ticker
"""

# The last bar printed for each (ticker, market date), used to mark rows
# whose price is a carry-forward rather than a fresh print.
LAST_MARK_PER_TICKER_DATE = """
    SELECT ticker, asof_date, is_stale
    FROM prices p
    WHERE bar_ts = (
        SELECT MAX(bar_ts) FROM prices q
        WHERE q.ticker = p.ticker AND q.asof_date = p.asof_date
    )
"""

# prices only, deliberately: fx_rates contains a synthetic USD row whose
# bar_ts is the fetch time, which would always read as brand new.
NEWEST_BAR = "SELECT MAX(bar_ts) AS newest FROM prices"


def format_usd(value) -> str:
    """Thousands-separated, fixed 2dp, negatives in accounting parens.

    Values that round to zero render as a plain "0.00" — "(0.00)" would
    imply a negative that isn't there.
    """
    if value is None:
        return "—"
    if abs(value) < 0.005:
        return "0.00"
    if value < 0:
        return f"({abs(value):,.2f})"
    return f"{value:,.2f}"


def sign_class(value) -> str:
    """CSS class carrying colour. Reinforcement only — the parentheses in
    `format_usd` are what actually communicate the sign."""
    if value is None or abs(value) < 0.005:
        return "zero"
    return "pos" if value > 0 else "neg"


def format_recon_diff(value) -> str:
    """Reconciliation residual.

    Deliberately not the 2dp used for money columns: a clean book has
    residuals around 1e-11, and 2dp would flatten every one of them to
    "0.00", hiding exactly the signal this badge exists to show.
    """
    if value is None:
        return "—"
    magnitude = abs(value)
    if magnitude < 0.0001:
        return "<0.0001"
    return f"{magnitude:,.4f}"


def humanize_duration(seconds) -> str | None:
    """A bare duration phrase with no tense — callers append "ago" (for
    an event) or "old" (for data). Returns None for an unknown age."""
    if seconds is None:
        return None
    if seconds < 10:
        return "a few seconds"
    if seconds < 60:
        return f"{int(seconds)} seconds"

    minutes = int(seconds // 60)
    if minutes == 1:
        return "1 minute"
    if minutes < 60:
        return f"{minutes} minutes"

    hours, rem_minutes = divmod(minutes, 60)
    if hours < 24:
        hour_word = "1 hour" if hours == 1 else f"{hours} hours"
        if rem_minutes == 0:
            return hour_word
        minute_word = "1 minute" if rem_minutes == 1 else f"{rem_minutes} minutes"
        return f"{hour_word} {minute_word}"

    days, rem_hours = divmod(hours, 24)
    day_word = "1 day" if days == 1 else f"{days} days"
    # Keep the hours: a Friday close read on a Sunday afternoon is
    # ~1d19h, and flooring that to a bare "1 day" understates the age of
    # the marks by nearly a full day.
    if rem_hours == 0 or days >= 7:
        return day_word
    hour_word = "1 hour" if rem_hours == 1 else f"{rem_hours} hours"
    return f"{day_word} {hour_word}"


def staleness_level(age_seconds, refresh_seconds: int) -> str:
    """'ok' | 'warn' | 'stale' purely on age. Callers decide whether an
    aged mark is actually a fault (see `_marks_state`)."""
    if age_seconds is None:
        return "stale"
    if age_seconds > STALE_MULTIPLE * refresh_seconds:
        return "stale"
    if age_seconds > WARN_MULTIPLE * refresh_seconds:
        return "warn"
    return "ok"


def _parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _money(value) -> dict:
    return {
        "value": value,
        "display": format_usd(value),
        "sign": sign_class(value),
    }


def _fetch_state(conn, now: datetime) -> dict:
    """Health of the *poller*. Distinct from the age of the marks — see
    `_marks_state`. This is what tells you ingestion is alive; it says
    nothing about whether the market has moved."""
    raw = get_state(conn, "last_successful_fetch")
    last_error = get_state(conn, "last_error") or ""
    last_dt = _parse_ts(raw)

    age_seconds = None
    hkt_display = "—"
    if last_dt is not None:
        age_seconds = max(0.0, (now - last_dt).total_seconds())
        hkt_display = last_dt.astimezone(HKT).strftime("%Y-%m-%d %H:%M:%S")

    duration = humanize_duration(age_seconds)
    return {
        "last_successful_fetch": raw,
        "last_successful_fetch_hkt": hkt_display,
        "age_seconds": None if age_seconds is None else int(age_seconds),
        "age_seconds_display": "—" if age_seconds is None else f"{int(age_seconds):,}",
        "age_words": f"{duration} ago" if duration else "never fetched",
        "last_error": last_error,
    }


def _marks_state(conn, venues: dict, refresh_seconds: int, now: datetime) -> dict:
    """Age and trustworthiness of the underlying market data.

    Four states, because "old" and "wrong" are not the same thing:

      ok      a venue is open and the newest bar is recent
      warn    a venue is open but the data has fallen behind
      stale   a venue is open and the data is badly behind
      closed  every venue is shut, so old marks are correct behaviour

    `closed` renders neutral, not amber: a book marked at Friday's close
    on a Sunday is doing exactly the right thing, and flagging it amber
    every weekend trains people to ignore the badge. Amber and red are
    reserved for data that is stale *while a market is open*, which is
    the only case that means something is broken.
    """
    row = fetch_one(conn, NEWEST_BAR)
    newest_raw = row["newest"] if row else None
    newest = _parse_ts(newest_raw)

    age_seconds = None
    newest_hkt = "—"
    if newest is not None:
        age_seconds = max(0.0, (now - newest).total_seconds())
        newest_hkt = newest.astimezone(HKT).strftime("%Y-%m-%d %H:%M:%S")

    any_open = any(status == "open" for status in venues.values())
    duration = humanize_duration(age_seconds)
    age_words = f"{duration} old" if duration else "no marks"

    if newest is None:
        level, label, detail = "stale", "No data", "no price bars stored"
    elif not any_open:
        level = "closed"
        label = "Closed"
        detail = f"marked at last traded price · {age_words}"
    else:
        level = staleness_level(age_seconds, refresh_seconds)
        label = {"ok": "Live", "warn": "Delayed", "stale": "Stale"}[level]
        detail = f"data {age_words}"

    return {
        "newest_bar_ts": newest_raw,
        "newest_bar_hkt": newest_hkt,
        "age_seconds": None if age_seconds is None else int(age_seconds),
        "age_words": age_words,
        "level": level,
        "level_label": label,
        "detail": detail,
        "any_venue_open": any_open,
        "warn_after_seconds": WARN_MULTIPLE * refresh_seconds,
        "stale_after_seconds": STALE_MULTIPLE * refresh_seconds,
    }


def _asof_spread(rows) -> dict:
    """How the book's market dates are distributed across venues.

    Positions settle on their own venue's calendar, so a single date is
    the exception, not the rule. Report the spread instead of picking one
    and implying it applies to everything.
    """
    by_date: dict[str, set] = {}
    for row in rows:
        venue = CURRENCY_VENUE.get(row["currency"], row["currency"])
        by_date.setdefault(row["asof_date"], set()).add(venue)

    dates = sorted(by_date, reverse=True)
    groups = [{"asof_date": d, "venues": sorted(by_date[d])} for d in dates]

    if not dates:
        return {"dates": [], "groups": [], "display": "—", "compact": "—", "mixed": False}

    if len(dates) == 1:
        only = dates[0]
        return {
            "dates": dates,
            "groups": groups,
            "display": only,
            "compact": only,
            "mixed": False,
        }

    display = " / ".join(
        f"{g['asof_date']} ({', '.join(g['venues'])})" for g in groups
    )
    return {
        "dates": dates,
        "groups": groups,
        "display": display,
        "compact": f"{dates[-1]} – {dates[0]}",
        "mixed": True,
    }


def build_summary(conn, refresh_seconds: int, now: datetime | None = None) -> dict:
    """Everything the dashboard renders, in one pass over the DB."""
    now = now or datetime.now(timezone.utc)

    venues = market_status(now)
    summary = {
        "generated_at": now.isoformat(timespec="seconds"),
        "refresh_seconds": refresh_seconds,
        "fetch": _fetch_state(conn, now),
        "marks": _marks_state(conn, venues, refresh_seconds, now),
        "venues": [
            {"venue": v, "status": venues.get(v, "unknown")} for v in VENUE_ORDER
        ],
        "leg_labels": [label for _, label in LEGS],
        "asof": _asof_spread([]),
        "rows": [],
        "kpis": [],
        "totals": {},
        "recon": {"ok": True, "breaks": 0, "max_abs_diff": None, "display": "—"},
        "empty": True,
    }

    attribution = fetch_all(conn, LATEST_ATTRIBUTION_PER_TICKER)
    if not attribution:
        return summary

    stale_by_key = {
        (r["ticker"], r["asof_date"]): bool(r["is_stale"])
        for r in fetch_all(conn, LAST_MARK_PER_TICKER_DATE)
    }

    totals = {column: 0.0 for column, _ in LEGS}
    totals["total_pnl"] = 0.0
    breaks = 0
    max_abs_diff = 0.0

    for record in attribution:
        legs = {}
        for column, _ in LEGS:
            value = record[column]
            legs[column] = _money(value)
            totals[column] += value

        totals["total_pnl"] += record["total_pnl"]
        recon_ok = bool(record["recon_ok"])
        if not recon_ok:
            breaks += 1
        max_abs_diff = max(max_abs_diff, abs(record["recon_diff"] or 0.0))

        summary["rows"].append(
            {
                "ticker": record["ticker"],
                "name": record["name"],
                "currency": record["currency"],
                "venue": CURRENCY_VENUE.get(record["currency"], record["currency"]),
                "asof_date": record["asof_date"],
                "shares": record["shares"],
                "shares_display": f"{record['shares']:,.0f}",
                "legs": [legs[column] for column, _ in LEGS],
                "total": _money(record["total_pnl"]),
                "is_stale": stale_by_key.get(
                    (record["ticker"], record["asof_date"]), False
                ),
                "recon_ok": recon_ok,
            }
        )

    summary["empty"] = False
    summary["asof"] = _asof_spread(summary["rows"])
    summary["totals"] = {
        "legs": [_money(totals[column]) for column, _ in LEGS],
        "total": _money(totals["total_pnl"]),
        "positions": len(summary["rows"]),
        "stale_rows": sum(1 for r in summary["rows"] if r["is_stale"]),
    }

    summary["kpis"] = [
        {"key": "total_pnl", "label": "Total P&L", **_money(totals["total_pnl"])}
    ] + [
        {"key": column, "label": label, **_money(totals[column])}
        for column, label in LEGS
    ]

    summary["recon"] = {
        "ok": breaks == 0,
        "breaks": breaks,
        "max_abs_diff": max_abs_diff,
        "display": format_recon_diff(max_abs_diff),
        "label": "Reconciled" if breaks == 0 else f"{breaks} break{'s' if breaks != 1 else ''}",
    }

    return summary
