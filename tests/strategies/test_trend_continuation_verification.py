"""
Updated test script for trend_continuation_v1 verification (v1.1).
"""
import unittest
from unittest.mock import MagicMock
import logging
import sys
import os
from datetime import datetime, timedelta

# Add project root to path
sys.path.append(os.getcwd())

from strategies.plugins.futures.active.trend_continuation_v1 import TrendContinuationV1

class TestTrendContinuationV1(unittest.TestCase):
    def setUp(self):
        self.strategy = TrendContinuationV1()
        
        # Explicit mocks
        self.context = MagicMock()
        self.context.config = {"params": {
            "shadow_mode": False,
            "max_hold_minutes": 30,
            "min_score": 70,
            "max_breakout": 0.15,
            "min_vol_spike": 1.2
        }}
        
        self.market = MagicMock()
        self.position = MagicMock()
        self.context.market = self.market
        self.context.position = self.position
        
        self.strategy.init(self.context)
        
        # Mock market data
        self.start_time = datetime(2026, 5, 4, 13, 0, 0)
        self.bar = {
            "Close": 41000,
            "vwap": 40950,
            "score": 80,
            "mom_state": 3,
            "ema_fast": 40980,
            "ema_slow": 40960,
            "breakout_strength": 0.1,
            "volume_spike": 1.5,
            "atr": 50,
            "regime": "STRONG",
            "recent_high": 41050,
            "timestamp": self.start_time.isoformat()
        }
        self.market.last_bar = self.bar
        self.position.size = 0
        self.position.unrealized_pnl = 0

    def test_entry_signal(self):
        # Normal entry condition
        signal = self.strategy.on_bar(self.context)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "BUY")
        self.assertEqual(signal.reason, "TREND_CONTINUATION_SCOUT")

    def test_shadow_mode(self):
        self.strategy.shadow_mode = True
        with self.assertLogs('strategies.plugins.futures.active.trend_continuation_v1', level='INFO') as cm:
            signal = self.strategy.on_bar(self.context)
            # Calibration 1: Shadow mode returns HOLD to stop router
            self.assertIsNotNone(signal)
            self.assertEqual(signal.action, "HOLD")
            self.assertEqual(signal.reason, "SHADOW_BUY_TRIGGERED")
            self.assertTrue(any("shadow_signal=BUY" in line for line in cm.output))

    def test_time_stop(self):
        # 1. First bar: Enter position
        self.position.size = 1
        self.position.unrealized_pnl = -10 # Losing
        self.position.entry_price = 41000
        
        # Initial call to set entry state
        self.strategy.on_bar(self.context)
        
        # 2. Advance time by 30 mins
        self.bar["timestamp"] = (self.start_time + timedelta(minutes=30)).isoformat()
        
        # 3. Check for exit
        signal = self.strategy.on_bar(self.context)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "EXIT")
        self.assertEqual(signal.reason, "TIME_STOP_CONTINUATION")

    def test_skip_reason_logging(self):
        # Score too low
        self.bar["score"] = 50
        with self.assertLogs('strategies.plugins.futures.active.trend_continuation_v1', level='INFO') as cm:
            signal = self.strategy.on_bar(self.context)
            self.assertIsNone(signal)
            self.assertTrue(any("SCORE_TOO_LOW" in line for line in cm.output))

if __name__ == "__main__":
    unittest.main()
