# 2026-10-20 Hermes Agent: guard Taipei-wall-time interpretation for mixed HVWAP timestamps.
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from strategies.plugins.futures.active.mts_hvwap_candidate import (
    aggregate_completed_bars,
    to_epoch_secs,
)


def test_naive_pandas_timestamp_matches_naive_datetime_and_aware_taipei():
    naive_datetime = datetime(2026, 10, 20, 15, 35)
    naive_timestamp = pd.Timestamp(naive_datetime)
    aware_taipei = naive_datetime.replace(tzinfo=ZoneInfo("Asia/Taipei"))

    epochs = [to_epoch_secs(value) for value in (
        naive_timestamp, naive_datetime, aware_taipei,
    )]

    assert epochs[0] == epochs[1] == epochs[2]


def test_mixed_naive_timestamp_bars_aggregate_seventy_five_to_twenty_two():
    first_close = datetime(2026, 10, 20, 9, 5)
    closes = [first_close + timedelta(minutes=5 * index) for index in range(66)]
    bars = [
        {
            "ts": pd.Timestamp(close) if index % 2 else close,
            "open": 100 + index,
            "high": 101 + index,
            "low": 99 + index,
            "close": 100 + index,
            "volume": 1,
        }
        for index, close in enumerate(closes)
    ]
    bars.extend(
        {
            "ts": close if index % 2 else pd.Timestamp(close),
            "open": 100 + index,
            "high": 101 + index,
            "low": 99 + index,
            "close": 100 + index,
            "volume": 1,
        }
        for index, close in enumerate(closes[:9])
    )

    result = aggregate_completed_bars(bars, datetime(2026, 10, 20, 14, 35))

    assert len(bars) == 75
    assert len(result) == 22
