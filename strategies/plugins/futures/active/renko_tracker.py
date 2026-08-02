# 2026-08-02 Antigravity: Production RenkoTracker with Canonical Custom Brick Events & Telemetry
import math
import time
import json
import os
import logging
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Tuple, List, Optional, Dict, Any

logger = logging.getLogger(__name__)
TAIPEI_TZ = timezone(timedelta(hours=8))

@dataclass
class RenkoBrickEvent:
    """Item One: Immutable Canonical Brick Event Schema"""
    brick_sequence: int
    created_at: str
    open: float
    close: float
    high: float
    low: float
    trend: int
    trend_label: str
    is_reversal: bool
    brick_size: float
    bricks_created_this_tick: int
    input_price: float
    price_source: str
    position_side: str
    position_effect: str
    trade_id: str
    single_leg_episode_id: str
    generation_id: int
    signal_emitted: bool = False
    signal_reason: Optional[str] = None
    signal_event_id: Optional[str] = None
    data_quality: str = "VALID"
    recovery_mode: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RenkoTracker:
    def __init__(
        self,
        anchor_price: float,
        brick_size: float = 10.0,
        symbol: str = "TMF",
        position_side: str = "LONG",
        price_source: str = "EXECUTABLE_BID",
        episode_id: str = "",
        trade_id: str = "",
        initialized_phase: str = "SINGLE_LEG",
        max_bricks_per_tick: int = 5,
        max_price_jump_points: float = 50.0,
    ):
        self.symbol = symbol
        self.position_side = position_side.upper()
        self.price_source = price_source
        self.episode_id = episode_id or trade_id or "EPISODE_0"
        self.trade_id = trade_id or self.episode_id
        self.initialized_phase = initialized_phase
        self.initialized_at = time.time()
        self.max_bricks_per_tick = max_bricks_per_tick
        self.max_price_jump_points = max_price_jump_points
        
        raw_size = max(float(brick_size), 0.5)
        self.locked_brick_size = round(raw_size * 4.0) / 4.0
        self.brick_size = self.locked_brick_size
        
        self.trend = 0
        self.anchor_price = float(anchor_price)
        self.renko_open = float(anchor_price)
        self.renko_close = float(anchor_price)
        self.total_bricks = 0
        self.brick_sequence = 0
        self.generation_id = 1
        
        self._recent_bricks = deque(maxlen=100)
        self._persisted_brick_keys = set()
        self.telemetry_failure_count = 0
        
        self.last_tick_bricks_created = 0
        self.last_tick_jump_points = 0.0
        self.last_tick_capped = False
        self.last_rejection_reason = None
        self.last_reversal_timestamp = None
        self.last_signal_event_id = None
        self.last_signal_at = None
        self.last_signal_reason = None

    def update_brick_size(self, atr: float, multiplier: float = 0.5, min_floor: float = 2.0):
        pass

    def _determine_position_effect(self, trend: int) -> str:
        if self.position_side == "LONG":
            return "FAVORABLE" if trend == 1 else "ADVERSE"
        else:
            return "FAVORABLE" if trend == -1 else "ADVERSE"

    def _create_and_store_brick(
        self,
        b_open: float,
        b_close: float,
        trend: int,
        is_reversal: bool,
        bricks_created: int,
        input_price: float,
        signal_emitted: bool = False,
        signal_reason: Optional[str] = None,
        signal_event_id: Optional[str] = None
    ) -> RenkoBrickEvent:
        self.brick_sequence += 1
        self.total_bricks += 1
        
        created_at_str = datetime.now(TAIPEI_TZ).isoformat()
        effect = self._determine_position_effect(trend)
        
        event = RenkoBrickEvent(
            brick_sequence=self.brick_sequence,
            created_at=created_at_str,
            open=b_open,
            close=b_close,
            high=max(b_open, b_close),
            low=min(b_open, b_close),
            trend=trend,
            trend_label="UP" if trend == 1 else "DOWN",
            is_reversal=is_reversal,
            brick_size=self.locked_brick_size,
            bricks_created_this_tick=bricks_created,
            input_price=input_price,
            price_source=self.price_source,
            position_side=self.position_side,
            position_effect=effect,
            trade_id=self.trade_id,
            single_leg_episode_id=self.episode_id,
            generation_id=self.generation_id,
            signal_emitted=signal_emitted,
            signal_reason=signal_reason,
            signal_event_id=signal_event_id
        )
        
        self._recent_bricks.append(event.to_dict())
        self._append_brick_telemetry(event)
        return event

    def _append_brick_telemetry(self, event: RenkoBrickEvent):
        dedupe_key = f"{event.generation_id}:{event.single_leg_episode_id}:{event.brick_sequence}"
        if dedupe_key in self._persisted_brick_keys:
            return
        
        try:
            date_str = datetime.now(TAIPEI_TZ).strftime("%Y%m%d")
            telemetry_dir = os.path.join("data", "telemetry", "renko_bricks", date_str)
            os.makedirs(telemetry_dir, exist_ok=True)
            filepath = os.path.join(telemetry_dir, f"{self.symbol}_bricks.jsonl")
            
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            
            self._persisted_brick_keys.add(dedupe_key)
        except Exception as e:
            self.telemetry_failure_count += 1
            logger.error("[RENKO_TELEMETRY_ERR] Failed to append brick event: %s", e)

    def add(self, price: float) -> Tuple[int, int, Dict[str, Any]]:
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
            "brick_events": []
        }

        if price is None or math.isnan(price) or math.isinf(price) or price <= 0:
            self.last_rejection_reason = "INVALID_PRICE_TICK"
            meta["rejection_reason"] = "INVALID_PRICE_TICK"
            return 0, self.trend, meta

        price = float(price)
        
        if self.trend != 0:
            jump = abs(price - self.renko_close)
            self.last_tick_jump_points = jump
            meta["jump_points"] = jump
            if jump > self.max_price_jump_points:
                self.last_rejection_reason = f"SINGLE_TICK_JUMP_EXCEEDED ({jump:.1f} > {self.max_price_jump_points:.1f})"
                meta["rejection_reason"] = self.last_rejection_reason
                return 0, self.trend, meta

        if self.trend == 0:
            diff = price - self.renko_close
            if abs(diff) >= self.locked_brick_size:
                raw_num = int(abs(diff) // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["capped"] = True
                
                target_trend = 1 if diff > 0 else -1
                self.trend = target_trend
                
                for b_i in range(num_bricks):
                    b_open = self.renko_close
                    b_close = b_open + (self.locked_brick_size if target_trend == 1 else -self.locked_brick_size)
                    self.renko_open = b_open
                    self.renko_close = b_close
                    event = self._create_and_store_brick(
                        b_open=b_open,
                        b_close=b_close,
                        trend=target_trend,
                        is_reversal=False,
                        bricks_created=1 if target_trend == 1 else -1,
                        input_price=price
                    )
                    meta["brick_events"].append(event.to_dict())

                self.last_tick_bricks_created = num_bricks if diff > 0 else -num_bricks
                meta["bricks_created"] = self.last_tick_bricks_created
                return self.last_tick_bricks_created, self.trend, meta
            return 0, 0, meta

        if self.trend == 1:
            if price >= self.renko_close + self.locked_brick_size:
                diff = price - self.renko_close
                raw_num = int(diff // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["capped"] = True

                for b_i in range(num_bricks):
                    b_open = self.renko_close
                    b_close = b_open + self.locked_brick_size
                    self.renko_open = b_open
                    self.renko_close = b_close
                    event = self._create_and_store_brick(
                        b_open=b_open,
                        b_close=b_close,
                        trend=1,
                        is_reversal=False,
                        bricks_created=1,
                        input_price=price
                    )
                    meta["brick_events"].append(event.to_dict())

                self.last_tick_bricks_created = num_bricks
                meta["bricks_created"] = num_bricks
                return num_bricks, self.trend, meta


            elif price <= self.renko_open - self.locked_brick_size:
                diff = (self.renko_open - self.locked_brick_size) - price
                raw_num = 1 + int(diff // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["rejection_reason"] = True

                self.trend = -1
                self.last_reversal_timestamp = time.time()
                is_adverse = (self.position_side == "LONG")
                meta["is_adverse_reversal"] = is_adverse
                
                b_open = self.renko_open
                b_close = b_open - self.locked_brick_size
                self.renko_open = b_open
                self.renko_close = b_close
                
                sig_event_id = None
                if is_adverse:
                    sig_event_id = f"{self.trade_id}:{self.generation_id}:{self.brick_sequence + 1}:-1"
                    self.last_signal_event_id = sig_event_id
                    self.last_signal_at = datetime.now(TAIPEI_TZ).isoformat()
                    self.last_signal_reason = f"{self.position_side}_ADVERSE_DOWN_REVERSAL"
                
                event = self._create_and_store_brick(
                    b_open=b_open,
                    b_close=b_close,
                    trend=-1,
                    is_reversal=True,
                    bricks_created=-1,
                    input_price=price,
                    signal_emitted=is_adverse,
                    signal_reason=self.last_signal_reason,
                    signal_event_id=sig_event_id
                )
                meta["brick_events"].append(event.to_dict())

                for b_i in range(1, num_bricks):
                    b_open = self.renko_close
                    b_close = b_open - self.locked_brick_size
                    self.renko_open = b_open
                    self.renko_close = b_close
                    c_event = self._create_and_store_brick(
                        b_open=b_open,
                        b_close=b_close,
                        trend=-1,
                        is_reversal=False,
                        bricks_created=-1,
                        input_price=price
                    )
                    meta["brick_events"].append(c_event.to_dict())

                self.last_tick_bricks_created = -num_bricks
                meta["rejection_reason"] = -num_bricks
                return -num_bricks, self.trend, meta

        elif self.trend == -1:
            if price <= self.renko_close - self.locked_brick_size:
                diff = self.renko_close - price
                raw_num = int(diff // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["capped"] = True

                for b_i in range(num_bricks):
                    b_open = self.renko_close
                    b_close = b_open - self.locked_brick_size
                    self.renko_open = b_open
                    self.renko_close = b_close
                    event = self._create_and_store_brick(
                        b_open=b_open,
                        b_close=b_close,
                        trend=-1,
                        is_reversal=False,
                        bricks_created=-1,
                        input_price=price
                    )
                    if not isinstance(meta.get("rejection_reason"), list):
                        meta["rejection_reason"] = []
                    if event is not None:
                        meta["brick_events"].append(event.to_dict())
                    else:
                        meta["brick_events"].append({"brick_sequence": self.brick_sequence, "capped": True})

                self.last_tick_bricks_created = -num_bricks
                meta["rejection_reason"] = -num_bricks  # capped brick count
                return -num_bricks, self.trend, meta

            elif price >= self.renko_open + self.locked_brick_size:
                diff = price - (self.renko_open + self.locked_brick_size)
                raw_num = 1 + int(diff // self.locked_brick_size)
                num_bricks = min(raw_num, self.max_bricks_per_tick)
                if raw_num > self.max_bricks_per_tick:
                    self.last_tick_capped = True
                    meta["capped"] = True

                self.trend = 1
                self.last_reversal_timestamp = time.time()
                is_adverse = (self.position_side == "SHORT")
                meta["is_adverse_reversal"] = is_adverse
                
                b_open = self.renko_open
                b_close = b_open + self.locked_brick_size
                self.renko_open = b_open
                self.renko_close = b_close


                sig_event_id = None
                if is_adverse:
                    sig_event_id = f"{self.trade_id}:{self.generation_id}:{self.brick_sequence + 1}:1"
                    self.last_signal_event_id = sig_event_id
                    self.last_signal_at = datetime.now(TAIPEI_TZ).isoformat()
                    self.last_signal_reason = f"{self.position_side}_ADVERSE_UP_REVERSAL"

                event = self._create_and_store_brick(
                    b_open=b_open,
                    b_close=b_close,
                    trend=1,
                    is_reversal=True,
                    bricks_created=1,
                    input_price=price,
                    signal_emitted=is_adverse,
                    signal_reason=self.last_signal_reason,
                    signal_event_id=sig_event_id
                )
                meta["brick_events"].append(event.to_dict())

                for b_i in range(1, num_bricks):
                    b_open = self.renko_close
                    b_close = b_open + self.locked_brick_size
                    self.renko_open = b_open
                    self.renko_close = b_close
                    c_event = self._create_and_store_brick(
                        b_open=b_open,
                        b_close=b_close,
                        trend=1,
                        is_reversal=False,
                        bricks_created=1,
                        input_price=price
                    )
                    meta["brick_events"].append(c_event.to_dict())


                self.last_tick_bricks_created = num_bricks
                meta["bricks_created"] = num_bricks
                return num_bricks, self.trend, meta


        return 0, self.trend, meta

    def get_recent_bricks(self, count: int = 50) -> List[Dict[str, Any]]:
        bricks = list(self._recent_bricks)
        return bricks[-count:] if len(bricks) > count else bricks

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "mode": "SINGLE_LEG_SHADOW",
            "capability_available": True,
            "tracker_initialized": self.total_bricks > 0 or self.trend != 0,
            "recovery_ready": self.total_bricks > 0,
            "symbol": self.symbol,
            "position_side": self.position_side,
            "price_source": self.price_source,
            "episode_id": self.episode_id,
            "trade_id": self.trade_id,
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
            "last_signal_event_id": self.last_signal_event_id,
            "last_signal_at": self.last_signal_at,
            "last_signal_reason": self.last_signal_reason,
            "telemetry_failure_count": self.telemetry_failure_count,
            "recent_bricks": self.get_recent_bricks(50)
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RenkoTracker":
        tracker = cls(
            anchor_price=float(data.get("anchor_price", 0.0)),
            brick_size=float(data.get("locked_brick_size", 10.0)),
            symbol=str(data.get("symbol", "TMF")),
            position_side=str(data.get("position_side", "LONG")),
            price_source=str(data.get("price_source", "EXECUTABLE_BID")),
            episode_id=str(data.get("episode_id", "")),
            trade_id=str(data.get("trade_id", "")),
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
        tracker.last_signal_event_id = data.get("last_signal_event_id")
        tracker.last_signal_at = data.get("last_signal_at")
        tracker.last_signal_reason = data.get("last_signal_reason")
        tracker.telemetry_failure_count = int(data.get("telemetry_failure_count", 0))
        
        recent = data.get("recent_bricks", [])
        if isinstance(recent, list):
            for b in recent:
                if isinstance(b, dict):
                    tracker._recent_bricks.append(b)
                    key = f"{b.get('generation_id', 1)}:{b.get('single_leg_episode_id', '')}:{b.get('brick_sequence', 0)}"
                    tracker._persisted_brick_keys.add(key)

        return tracker
