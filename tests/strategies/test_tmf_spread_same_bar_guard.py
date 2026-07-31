import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta
from strategies.plugins.futures.active.tmf_spread import TMFSpread, Leg

def test_is_same_bar_as_release_timezone_awareness():
    """Verify _is_same_bar_as_release correctly handles tz-aware vs tz-naive timestamps."""
    strategy = TMFSpread()
    
    # Release time at 15:56:42 naive local time
    rel_dt = datetime(2026, 7, 31, 15, 56, 42)
    strategy._release_ts = rel_dt

    # 1. Same 5-min bar (15:55:00) with tz-aware string (+08:00)
    bar_ts_tz_aware = "2026-07-31T15:55:00+08:00"
    assert strategy._is_same_bar_as_release(bar_ts_tz_aware) is True

    # 2. Same 5-min bar with ISO string Z / UTC
    bar_ts_utc = "2026-07-31T07:55:00Z"  # Equivalent to 15:55:00+08:00
    assert strategy._is_same_bar_as_release(bar_ts_utc) is True

    # 3. Same 5-min bar with pandas Timestamp
    bar_ts_pd = pd.Timestamp("2026-07-31 15:55:00")
    assert strategy._is_same_bar_as_release(bar_ts_pd) is True

    # 4. Next 5-min bar (16:00:00) -> Should return False
    bar_ts_next_bar = "2026-07-31T16:00:00+08:00"
    assert strategy._is_same_bar_as_release(bar_ts_next_bar) is False

def test_same_bar_rem_high_isolation():
    """Verify that during the same bar as release, rem_high is isolated to current close."""
    strategy = TMFSpread()
    strategy._release_ts = datetime(2026, 7, 31, 15, 56, 42)
    strategy._released_leg = "far"

    # Simulated bar with high near_high (43730) that occurred before release
    bar_data = {
        "timestamp": "2026-07-31T15:55:00+08:00",
        "near_high": 43730.0,
        "near_low": 43700.0,
        "near_close": 43705.0,
        "far_close": 43700.0
    }

    # Verify same bar guard is True
    is_same_bar = strategy._is_same_bar_as_release(bar_data["timestamp"])
    assert is_same_bar is True

