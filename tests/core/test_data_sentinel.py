"""
Unit tests for core/data_sentinel.py
Verifies market hour logic and gap detection accuracy for TMF.
"""
import pytest
import pandas as pd
from datetime import datetime, time
from core.data_sentinel import DataSentinel

def test_market_hours_logic():
    sentinel = DataSentinel()
    
    # Tuesday 10:00 (Day session) - Open
    assert sentinel.is_market_open(datetime(2026, 4, 7, 10, 0)) == True
    
    # Tuesday 14:00 (Post-day break) - Closed
    assert sentinel.is_market_open(datetime(2026, 4, 7, 14, 0)) == False
    
    # Tuesday 20:00 (Night session) - Open
    assert sentinel.is_market_open(datetime(2026, 4, 7, 20, 0)) == True
    
    # Saturday 03:00 (Friday night session) - Open
    assert sentinel.is_market_open(datetime(2026, 4, 11, 3, 0)) == True
    
    # Saturday 10:00 (Weekend) - Closed
    assert sentinel.is_market_open(datetime(2026, 4, 11, 10, 0)) == False
    
    # Sunday 20:00 (Weekend) - Closed
    assert sentinel.is_market_open(datetime(2026, 4, 12, 20, 0)) == False
    
    # Monday 02:00 (No Sunday night session) - Closed
    assert sentinel.is_market_open(datetime(2026, 4, 13, 2, 0)) == False

def test_gap_detection():
    sentinel = DataSentinel()
    
    # Create expected index for a specific Tuesday (Day + Night)
    # Day: 08:45-13:40 (last bar), Night: 15:00-04:55
    start = datetime(2026, 4, 7, 8, 45)
    end = datetime(2026, 4, 7, 23, 55)
    
    full_idx = sentinel.generate_expected_index(start, end)
    
    # Create DF with a gap: remove bars between 10:00 and 11:00
    gap_start = datetime(2026, 4, 7, 10, 0)
    gap_end = datetime(2026, 4, 7, 11, 0)
    
    df_with_gap = pd.DataFrame(index=full_idx)
    df_with_gap = df_with_gap.drop(df_with_gap.loc[gap_start:gap_end].index[:-1])
    
    gaps = sentinel.audit_gaps(df_with_gap)
    
    assert len(gaps) == 1
    # Gap should be from 10:00 to 10:55 (last missing bar)
    assert gaps[0][0] == gap_start
    assert gaps[0][1] == datetime(2026, 4, 7, 10, 55)

def test_gap_detection_across_sessions():
    sentinel = DataSentinel()
    
    # Gap across the 13:45 - 15:00 break
    start = datetime(2026, 4, 7, 13, 0)
    end = datetime(2026, 4, 7, 16, 0)
    
    full_idx = sentinel.generate_expected_index(start, end)
    
    # Remove last 2 bars of day session and first 2 bars of night session
    to_drop = [
        datetime(2026, 4, 7, 13, 35),
        datetime(2026, 4, 7, 13, 40),
        datetime(2026, 4, 7, 15, 0),
        datetime(2026, 4, 7, 15, 5)
    ]
    
    df_with_gap = pd.DataFrame(index=full_idx)
    df_with_gap = df_with_gap.drop(to_drop)
    
    gaps = sentinel.audit_gaps(df_with_gap)
    
    # Behavior: 2 gaps because of the session break
    assert len(gaps) == 2
    assert gaps[0] == (datetime(2026, 4, 7, 13, 35), datetime(2026, 4, 7, 13, 40))
    assert gaps[1] == (datetime(2026, 4, 7, 15, 0), datetime(2026, 4, 7, 15, 5))
