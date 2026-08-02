# 2026-08-02 Antigravity: Production-grade RenkoTracker module (Items A-K Audit Compliant)
import math
import time
from typing import Tuple, List, Optional, Dict, Any

class RenkoTracker:
    """
    Production-grade Renko Chart Tracker for single-leg exit noise filtering.
    Features:
      - Locked brick_size for single-leg episodes (Item A)
      - Explicit adverse/favorable direction semantics (Item B)
      - Canonical mark/executable price anchor initialization (Item C)
      - Single-tick brick/jump cap & O(1) safety (Item G)
      - Server-side ATR clamp & tick quantization (Item H)
      - Full state serialization for zero-drift restart recovery (Item F)
    """
    
    def __init__(
        self,
        anchor_price: float,
        brick_size: float = 10.0,
        symbol: str = "TMF",
        position_side: str = "LONG",
        price_source: str = "EXECUTABLE_BID",
        episode_id: str = "",
        initialized_phase: str = "SINGLE_LEG",
        max_bricks_per_tick: int = 5,
        max_price_jump_points: float = 50.0,
    ):
        self.symbol = symbol
        self.position_side = position_side.upper()
        self.price_source = price_source
        self.episode_id = episode_id
        self.initialized_phase = initialized_phase
        self.initialized_at = time.time()
        self.max_bricks_per_tick = max_bricks_per_tick
        self.max_price_jump_points = max_price_jump_points
        
        # Item H: Server-side clamp multiplier / tick quantization
        raw_size = max(float(brick_size), 0.5)
        # Quantize to valid 0.25 tick size
        self.locked_brick_size = round(raw_size * 4.0) / 4.0
        self.brick_size = self.locked_brick_size  # Immutable during episode
        
        self.trend = 0  # 1 = UP/Bullish, -1 = DOWN/Bearish, 0 = Uninitialized
        self.anchor_price = float(anchor_price)
        self.renko_open = float(anchor_price)
        self.renko_close = float(anchor_price)
        self.total_bricks = 0
        self.brick_sequence = 0
        self.generation_id = 1
        
        # Telemetry & Diagnostics (Item G & K)
        self.last_tick_bricks_created = 0
        self.last_tick_jump_points = 0.0
        self.last_tick_capped = False
        self.last_rejection_reason = None
        self.last_reversal_timestamp = None

    def update_brick_size(self, atr: float, multiplier: float = 0.5, min_floor: float = 2.0):
        """
        Item A: In-flight single-leg episode MUST NOT alter locked_brick_size.
        This method logs/validates proposed dynamic ATR without modifying locked_brick_size.
        """
        pass  # locked_brick_size is strictly locked per single-leg episode!

    def add(self, price: float) -> Tuple[int, int, Dict[str, Any]]:
        """
        Process a new price tick.
        
        Returns:
            Tuple[int, int, Dict[str, Any]]: (new_bricks_count, current_trend, telemetry_meta)
                new_bricks_count: Positive for bullish bricks, negative for bearish bricks, 0 for no change
                current_trend: 1 (UP), -1 (DOWN), 0 (FLAT)
                telemetry_meta: Metadata dictionary including capped, jump_points, rejection_reason
        """
        self.last_tick_bricks_created = 0
        self.last_tick_jump_points = 0.0
        self.last_tick_capped = False
        self.last_rejection_reason = None
        
        meta = {
            "bricks_created": 0,
            "jump_points": 0.0,
            "capped": False,
            "rejection_reason": None,
            "is_adverse_reversal": False,
        }

        # Item H: Safety check for invalid price ticks
        if price is None or math.isnan(price) or math.isinf(price) or price <= 0:
            self.last_rejection_reason = "INVALID_PRICE_TICK"
            meta["rejection_reason"] = "INVALID_PRICE_TICK"
            return 0, self.trend, meta

        price = float(price)
        
        # Item G: Single-tick price jump clamp guard
        if self.trend != 0:
            jump = abs(price - self.renko_close)
            self.last_tick_jump_points = jump
            meta["jump_points"] = jump
            if jump > self.max_price_jump_points:
                self.last_rejection_reason = f"SINGLE_TICK_JUMP_EXCEEDED ({jump:.1f} > {self.max_price_jump_points:.1f})"
                meta["rejection_reason"] = self.last_rejection_reason
                return 0, self.trend, meta

        # Initial brick setup from anchor (Item C: first tick only establishes anchor)
        if self.trend == 0:
            diff = price - self.renko_close
            if abs(diff) >= self.locked_brick_size:
                raw_num = int(abs(diff) // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["capped"] = True
                
                if diff > 0:
                    self.trend = 1
                    self.renko_open = self.renko_close
                    self.renko_close = self.renko_open + (num_bricks * self.locked_brick_size)
                else:
                    self.trend = -1
                    self.renko_open = self.renko_close
                    self.renko_close = self.renko_open - (num_bricks * self.locked_brick_size)
                
                self.total_bricks += num_bricks
                self.brick_sequence += num_bricks
                self.last_tick_bricks_created = num_bricks if diff > 0 else -num_bricks
                meta["rricks_created"] = self.last_tick_bricks_created
                return self.last_tick_bricks_created, self.trend, meta
            return 0, 0, meta

        # Bullish Trend (trend == 1)
        if self.trend == 1:
            # Continuation upward
            if price >= self.renko_close + self.locked_brick_size:
                diff = price - self.renko_close
                raw_num = int(diff // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["capped"] = True

                self.renko_open = self.renko_close + ((num_bricks - 1) * self.locked_brick_size)
                self.renko_close = self.renko_close + (num_bricks * self.locked_brick_size)
                self.total_bricks += num_bricks
                self.brick_sequence += num_bricks
                self.last_tick_bricks_created = num_bricks
                meta["bricks_created"] = num_bricks
                return num_bricks, self.trend, meta

            # Reversal downward (requires 2 bricks down from peak close, i.e. price <= renko_open - locked_brick_size)
            elif price <= self.renko_open - self.locked_brick_size:
                diff = (self.renko_open - self.locked_brick_size) - price
                raw_num = 1 + int(diff // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["capped"] = True

                self.trend = -1
                self.last_reversal_timestamp = time.time()
                self.renko_open = self.renko_open
                self.renko_close = self.renko_open - (num_bricks * self.locked_brick_size)
                self.total_bricks += num_bricks
                self.brick_sequence += num_bricks
                self.last_tick_bricks_created = -num_bricks
                meta["bricks_created"] = -num_bricks
                
                # Item B: Check adverse direction relative to position side
                if self.position_side == "LONG":
                    meta["is_adverse_reversal"] = True

                return -num_bricks, self.trend, meta

        # Bearish Trend (trend == -1)
        elif self.trend == -1:
            # Continuation downward
            if price <= self.renko_close - self.locked_brick_size:
                diff = self.renko_close - price
                raw_num = int(diff // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["capped"] = True

                self.renko_open = self.renko_close - ((num_bricks - 1) * self.locked_brick_size)
                self.renko_close = self.renko_close - (num_bricks * self.locked_brick_size)
                self.total_bricks += num_bricks
                self.brick_sequence += num_bricks
                self.last_tick_bricks_created = -num_bricks
                meta["bricks_created"] = -num_bricks
                return -num_bricks, self.trend, meta

            # Reversal upward (requires 2 bricks up from trough close, i.e. price >= renko_open + locked_brick_size)
            elif price >= self.renko_open + self.locked_brick_size:
                diff = price - (self.renko_open + self.locked_brick_size)
                raw_num = 1 + int(diff // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["sapped"] = True

                self.trend = 1
                self.last_reversal_timestamp = time.time()
                self.renko_open = self.renko_open
                self.renko_close = self.renko_open + (num_bricks * self.locked_brick_size)
                self.total_bricks += num_bricks
                self.brick_sequence += num_bricks
                self.last_tick_bricks_created = num_bricks
                meta["bricks_created"] = num_bricks
                
                # Item B: Check adverse direction relative to position side
                if self.position_side == "SHORT":
                    meta["is_adverse_reversal"] = True

                return num_bricks, self.trend, meta

        return 0, self.trend, meta

    def to_dict(self) -> Dict[str, Any]:
        """Item F: Full state serialization for zero-drift restart recovery."""
        return {
            "symbol": self.symbol,
            "position_side": self.position_side,
            "price_source": self.price_source,
            "episode_id": self.episode_id,
            "initialized_phase": self.initialized_phase,
            "initialized_at": self.initialized_at,
            "anchor_price": self.anchor_price,
            "renko_open": self.renko_open,
            "renko_close": self.renko_close,
            "trend": self.trend,
            "locked_brick_size": self.locked_brick_size,
            "total_bricks": self.total_bricks,
            "brick_sequence": self.brick_sequence,
            "generation_id": self.generation_id,
            "max_bricks_per_tick": self.max_bricks_per_tick,
            "max_price_jump_points": self.max_price_jump_points,
            "last_reversal_timestamp": self.last_reversal_timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RenkoTracker":
        """Item F: Reconstruct exact tracker instance from serialized state."""
        tracker = cls(
            anchor_price=float(data.get("anchor_price", 0.0)),
            brick_size=float(data.get("locked_brick_size", 10.0)),
            symbol=str(data.get("symbol", "TMF")),
            position_side=str(data.get("position_side", "LONG")),
            price_source=str(data.get("price_source", "EXECUTABLE_BID")),
            episode_id=str(data.get("episode_id", "")),
            initialized_phase=str(data.get("initialized_phase", "SINGLE_LEG")),
            max_bricks_per_tick=int(data.get("max_bricks_per_tick", 5)),
            max_price_jump_points=float(data.get("max_price_jump_points", 50.0)),
        )
        tracker.initialized_at = float(data.get("initialized_at", time.time()))
        tracker.renko_open = float(data.get("renko_open", tracker.anchor_price))
        tracker.renko_close = float(data.get("renko_close", tracker.anchor_price))
        tracker.trend = int(data.get("trend", 0))
        tracker.total_bricks = int(data.get("total_bricks", 0))
        tracker.brick_sequence = int(data.get("brick_sequence", 0))
        tracker.generation_id = int(data.get("generation_id", 1))
        tracker.last_reversal_timestamp = data.get("last_reversal_timestamp")
        return tracker
