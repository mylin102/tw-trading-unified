"""The exchange holiday calendar must exist — its absence caused a 4-day outage.

2026-09-25 09:38: config/holidays.json and config/holidays_custom.json did not
exist, so `fetch_holidays()` returned an EMPTY set and
`is_taifex_futures_market_open("2026-09-25 09:38")` said the market was OPEN.
The day session then had no data, the feed-stale guard fired, main.py exited
"for external supervisor", pm2 gave up after max_restarts=2 and the system
stayed down for four days (the watchdog that would have caught it did not
exist yet either).  09-25 (Mid-Autumn) and 09-28 (Teachers' Day) are both on
the official TAIFEX 2026 holiday schedule.
"""
import json
from datetime import datetime

from core.date_utils import (CUSTOM_HOLIDAYS_PATH, fetch_holidays,
                             is_taifex_futures_market_open)

OFFICIAL_2026 = {
    "2026-01-01", "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-02-19", "2026-02-20", "2026-02-27", "2026-02-28", "2026-04-03",
    "2026-04-04", "2026-04-05", "2026-04-06", "2026-05-01", "2026-06-19",
    "2026-09-25", "2026-09-28", "2026-10-09", "2026-10-10", "2026-10-25",
    "2026-10-26", "2026-12-25",
}


def _shipped():
    return set(json.loads(CUSTOM_HOLIDAYS_PATH.read_text(encoding="utf-8")))


def test_the_shipped_calendar_is_a_non_empty_date_list():
    data = _shipped()
    assert data, "an empty holiday calendar is exactly how a session day became 'open'"
    assert all(isinstance(d, str) and len(d) == 10 and d[4] == d[7] == "-" for d in data)


def test_the_official_2026_holidays_are_present():
    assert OFFICIAL_2026 <= _shipped()


def test_fetch_holidays_merges_the_shipped_calendar():
    assert OFFICIAL_2026 <= {str(x) for x in fetch_holidays()}


def test_the_exchange_is_closed_on_the_holidays_that_killed_it():
    for day in ("2026-09-25", "2026-09-28", "2026-10-09", "2026-10-26", "2026-02-17"):
        y, m, d = (int(x) for x in day.split("-"))
        assert is_taifex_futures_market_open(datetime(y, m, d, 10, 0)) is False, day


def test_real_trading_days_are_still_open():
    for day in ("2026-09-29", "2026-09-30", "2026-10-01", "2026-10-12"):
        y, m, d = (int(x) for x in day.split("-"))
        assert is_taifex_futures_market_open(datetime(y, m, d, 10, 0)) is True, day
