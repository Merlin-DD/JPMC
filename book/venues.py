"""Exchange calendars and currency->venue mapping.

Deliberately dependency-free: stdlib only, no yfinance, no Django, no
database. The web request path needs `market_status()` and `venue_tz()`
to render the dashboard header, and must not drag the market-data client
into a request just to find out what timezone Tokyo is in.

book/ingest.py re-uses these too — the calendar is shared, the yfinance
dependency is not.
"""

import logging
from datetime import datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

VENUES = {
    "NYSE": {"tz": ZoneInfo("America/New_York"), "open": dt_time(9, 30), "close": dt_time(16, 0)},
    "XETRA": {"tz": ZoneInfo("Europe/Berlin"), "open": dt_time(9, 0), "close": dt_time(17, 30)},
    "HKEX": {"tz": ZoneInfo("Asia/Hong_Kong"), "open": dt_time(9, 30), "close": dt_time(16, 0)},
    "TSE": {"tz": ZoneInfo("Asia/Tokyo"), "open": dt_time(9, 0), "close": dt_time(15, 0)},
}

# Each currency's home venue, used to derive asof_date (the market's local
# calendar date) from a UTC bar_ts. Approximate by design: e.g. a USD
# position may actually trade on NASDAQ rather than NYSE, but both share
# America/New_York, so the date derivation is correct either way.
CURRENCY_VENUE = {
    "USD": "NYSE",
    "HKD": "HKEX",
    "JPY": "TSE",
    "EUR": "XETRA",
}


def venue_tz(currency: str):
    """Timezone of the venue a currency's positions trade on."""
    venue = CURRENCY_VENUE.get(currency)
    if venue is None:
        logger.warning("no venue mapped for currency %s, using UTC for asof_date", currency)
        return timezone.utc
    return VENUES[venue]["tz"]


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
