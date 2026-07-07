"""Market hours unit tests — table-driven, no external deps.

Tests is_taifex_futures_market_open() with pinned datetimes.
"""
from datetime import datetime
import pytest
from core.date_utils import is_taifex_futures_market_open


# Pinned dates for 2026-07 week:
# Mon 07/06, Tue 07/07, Wed 07/08, Thu 07/09, Fri 07/10, Sat 07/11, Sun 07/12

TEST_CASES = [
    # (description, dt, expected_open)
    # ── Night session 00:00-05:00 — belongs to PREVIOUS trading day ──
    ("Tue 01:15 → Mon night → OPEN",   datetime(2026, 7, 7,  1, 15), True),
    ("Mon 01:15 → Sun night → CLOSED", datetime(2026, 7, 6,  1, 15), False),
    ("Sat 01:15 → Fri night → OPEN",   datetime(2026, 7, 11, 1, 15), True),
    ("Sun 01:15 → Sat night → CLOSED", datetime(2026, 7, 12, 1, 15), False),
    # ── Night session 15:00-23:59 — belongs to SAME day ──
    ("Fri 23:30 → OPEN",  datetime(2026, 7, 10, 23, 30), True),
    ("Sat 03:30 → Fri night → OPEN", datetime(2026, 7, 11, 3, 30), True),
    ("Mon 15:30 → OPEN",  datetime(2026, 7, 6,  15, 30), True),
    ("Sat 16:00 → CLOSED (weekend, no night session)", datetime(2026, 7, 11, 16, 0), False),
    # ── Day session 08:45-13:45 ──
    ("Tue 09:00 → OPEN",  datetime(2026, 7, 7,  9, 0),  True),
    ("Sat 09:00 → CLOSED", datetime(2026, 7, 11, 9, 0), False),
    # ── Between sessions (closed) ──
    ("Tue 14:00 → CLOSED (lunch break)", datetime(2026, 7, 7, 14, 0), False),
    ("Tue 06:00 → CLOSED (after night, before day)", datetime(2026, 7, 7, 6, 0), False),
]


@pytest.mark.parametrize("desc,dt,expected", TEST_CASES)
def test_market_open(desc, dt, expected):
    result = is_taifex_futures_market_open(dt)
    wd = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][dt.weekday()]
    assert result == expected, \
        f"{desc}: dt={dt} ({wd}) expected={expected} got={result}"
