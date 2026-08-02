# 2026-08-02 Antigravity: RenkoTracker module for noise-filtered single-leg exit tracking
import math
from typing import Tuple, List, Optional

class RenkoTracker:
    """
    Renko Chart Tracker for real-time price tick noise filtering.
    Maintains Renko bricks based on price movements and standard 2-brick reversal logic.
    """
    
    def __init__(self, anchor_price: float, brick_size: float = 10.0, symbol: str = "TMF"):
        self.symbol = symbol
        self.brick_size = float(max(brick_size, 0.5))
        self.trend = 0
        self.anchor_price = float(anchor_price)
        self.renko_open = float(anchor_price)
        self.renko_close = float(anchor_price)
        self.total_bricks = 0

    def update_brick_size(self, atr: float, multiplier: float = 1.0, min_floor: float = 2.0):
        """Dynamically update brick size based on current ATR."""
        if atr and atr > 0:
            new_size = max(float(atr) * float(multiplier), float(min_floor))
            self.brick_size = round(new_size, 2)

    def add(self, price: float) -> Tuple[int, int]:
        """
        Process a new price tick.
        
        Returns:
            Tuple[int, int]: (new_bricks_count, current_trend)
                new_bricks_count: Positive for bullish bricks, negative for bearish bricks, 0 for no change
                current_trend: 1 (UP), -1 (DOWN), 0 (FLAT)
        """
        if price is None or price <= 0:
            return 0, self.trend

        price = float(price)
        
        # Initial brick setup from anchor
        if self.trend == 0:
            diff = price - self.renko_close
            if abs(diff) >= self.brick_size:
                num_bricks = int(abs(diff) // self.brick_size)
                if diff > 0:
                    self.trend = 1
                    self.renko_open = self.renko_close
                    self.renko_close = self.renko_open + (num_bricks * self.brick_size)
                    self.total_bricks += num_bricks
                    return num_bricks, self.trend
                else:
                    self.trend = -1
                    self.renko_open = self.renko_close
                    self.renko_close = self.renko_open - (num_bricks * self.brick_size)
                    self.total_bricks += num_bricks
                    return -num_bricks, self.trend
            return 0, 0

        # Bullish Trend (trend == 1)
        if self.trend == 1:
            # Continuation upward
            if price >= self.renko_close + self.brick_size:
                diff = price - self.renko_close
                num_bricks = int(diff // self.brick_size)
                self.renko_open = self.renko_close + ((num_bricks - 1) * self.brick_size)
                self.renko_close = self.renko_close + (num_bricks * self.brick_size)
                self.total_bricks += num_bricks
                return num_bricks, self.trend
            # Reversal downward (requires 2 bricks down from peak close, i.e. price <= renko_open - brick_size)
            elif price <= self.renko_open - self.brick_size:
                diff = (self.renko_open - self.brick_size) - price
                num_bricks = 1 + int(diff // self.brick_size)
                self.trend = -1
                self.renko_open = self.renko_open
                self.renko_close = self.renko_open - (num_bricks * self.brick_size)
                self.total_bricks += num_bricks
                return -num_bricks, self.trend

        # Bearish Trend (trend == -1)
        elif self.trend == -1:
            # Continuation downward
            if price <= self.renko_close - self.brick_size:
                diff = self.renko_close - price
                num_bricks = int(diff // self.brick_size)
                self.renko_open = self.renko_close - ((num_bricks - 1) * self.brick_size)
                self.renko_close = self.renko_close - (num_bricks * self.brick_size)
                self.total_bricks += num_bricks
                return -num_bricks, self.trend
            # Reversal upward (requires 2 bricks up from trough close, i.e. price >= renko_open + brick_size)
            elif price >= self.renko_open + self.brick_size:
                diff = price - (self.renko_open + self.brick_size)
                num_bricks = 1 + int(diff // self.brick_size)
                self.trend = 1
                self.renko_open = self.renko_open
                self.renko_close = self.renko_open + (num_bricks * self.brick_size)
                self.total_bricks += num_bricks
                return num_bricks, self.trend

        return 0, self.trend
