"""
Data Sentinel — Detect and manage gaps in financial time-series data.
Specifically tailored for Taiwan Futures (TAIFEX) market hours.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import List, Tuple, Optional

import numpy as np
import pandas as pd


class DataSentinel:
    """Audits data integrity and identifies missing periods."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)

    def is_market_open(self, dt: datetime) -> bool:
        """Check if a given datetime is within Taiwan Futures trading hours."""
        # 1. Weekend check (Saturday morning is still night session)
        if dt.weekday() == 6:  # Sunday
            return False
        
        # Saturday: Night session ends at 05:00. No day session.
        if dt.weekday() == 5:
            return dt.time() < time(5, 0)

        # Monday morning: No trading before 08:45
        if dt.weekday() == 0 and dt.time() < time(8, 45):
            return False

        # 2. Daily Sessions
        t = dt.time()
        # Day session: 08:45 - 13:45
        if time(8, 45) <= t < time(13, 45):
            return True
        # Night session: 15:00 - 05:00 (next day)
        if t >= time(15, 0) or t < time(5, 0):
            return True
            
        return False

    def generate_expected_index(self, start: datetime, end: datetime, freq: str = "5min") -> pd.DatetimeIndex:
        """Generate a DatetimeIndex of all expected bars within market hours."""
        full_idx = pd.date_range(start, end, freq=freq)
        # Filter by market hours
        valid_mask = [self.is_market_open(dt) for dt in full_idx]
        return full_idx[valid_mask].tz_localize(None)

    def audit_gaps(self, df: pd.DataFrame, expected_freq: str = "5min") -> List[Tuple[datetime, datetime]]:
        """Identify missing periods in the DataFrame index."""
        if df.index.empty:
            return []
        
        # 1. Standardize actual index
        actual_idx = pd.to_datetime(df.index).tz_localize(None)
        start, end = actual_idx.min(), actual_idx.max()
        
        # 2. Generate expected index
        expected_idx = self.generate_expected_index(start, end, freq=expected_freq)
        
        # 3. Find missing (Difference)
        missing = sorted(list(set(expected_idx) - set(actual_idx)))
        if not missing:
            return []

        # 4. Group into continuous ranges
        gaps = []
        delta = pd.Timedelta(expected_freq)
        
        gap_start = missing[0]
        curr_end = missing[0]
        
        for i in range(1, len(missing)):
            if missing[i] - missing[i-1] <= delta:
                curr_end = missing[i]
            else:
                gaps.append((gap_start, curr_end))
                gap_start = missing[i]
                curr_end = missing[i]
        
        gaps.append((gap_start, curr_end))
        return gaps

    def merge_and_clean(self, base_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
        """Merge new data into base, removing duplicates and sorting."""
        if new_df.empty:
            return base_df
        
        combined = pd.concat([base_df, new_df])
        # Use index for duplicate detection
        combined = combined[~combined.index.duplicated(keep='first')]
        return combined.sort_index()

# Singleton
data_sentinel = DataSentinel()
