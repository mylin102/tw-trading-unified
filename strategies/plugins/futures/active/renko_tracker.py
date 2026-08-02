# 2026-08-02 Antigravity: Production RenkoTracker with Explicit Exception Invariants, Enhanced Telemetry Loader Counters & Flexible Sequence Contract

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
    generation_id: str
    source_receive_sequence: Optional[int] = None
    signal_emitted: bool = False
    signal_reason: Optional[str] = None
    signal_event_id: Optional[str] = None
    data_quality: str = "VALID"
    recovery_mode: bool = False

    def __post_init__(self):
        if isinstance(self.bricks_created_this_tick, bool) or not isinstance(self.bricks_created_this_tick, int):
            raise TypeError(f"bricks_created_this_tick must be int, got {type(self.bricks_created_this_tick)}")
        if self.bricks_created_this_tick < 1:
            raise ValueError(f"bricks_created_this_tick must be >= 1, got {self.bricks_created_this_tick}")

        if self.trend not in (1, -1):
            raise ValueError(f"trend must be 1 or -1, got {self.trend}")

        if isinstance(self.brick_sequence, bool) or not isinstance(self.brick_sequence, int) or self.brick_sequence < 1:
            raise ValueError(f"brick_sequence must be int >= 1, got {self.brick_sequence}")

        if not isinstance(self.brick_size, (int, float)) or self.brick_size <= 0:
            raise ValueError(f"brick_size must be > 0, got {self.brick_size}")

        if math.isclose(self.open, self.close):
            raise ValueError(f"close cannot equal open for a completed brick (open={self.open}, close={self.close})")

        if self.position_side not in ("LONG", "SHORT"):
            raise ValueError(f"invalid position_side: {self.position_side}")

        if self.position_effect not in ("FAVORABLE", "ADVERSE"):
            raise ValueError(f"invalid position_effect: {self.position_effect}")

        if self.source_receive_sequence is not None:
            if isinstance(self.source_receive_sequence, bool) or not isinstance(self.source_receive_sequence, int) or self.source_receive_sequence < 0:
                raise ValueError(f"source_receive_sequence must be int >= 0 or None, got {self.source_receive_sequence}")

        if not isinstance(self.generation_id, str):
            raise TypeError("generation_id must be str")
        if not isinstance(self.single_leg_episode_id, str):
            raise TypeError("single_leg_episode_id must be str")
        if not isinstance(self.trade_id, str):
            raise TypeError("trade_id must be str")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = 1
        return d


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
        episode_ordinal: int = 1,
        generation_id: str = "1",
        initialized_phase: str = "SINGLE_LEG",
        max_bricks_per_tick: int = 5,
        max_price_jump_points: float = 50.0,
    ):
        self.symbol = symbol
        self.position_side = position_side.upper()
        self.price_source = price_source
        self.trade_id = str(trade_id) if trade_id else "TRADE_0"
        self.episode_ordinal = int(episode_ordinal)
        
        if episode_id:
            self.episode_id = str(episode_id)
        else:
            self.episode_id = f"{self.trade_id}:single-leg:{self.episode_ordinal}"
        
        self.generation_id = str(generation_id)
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
        self.source_receive_sequence = 0
        
        self._recent_bricks = deque(maxlen=100)
        self._persisted_brick_keys = set()
        self.telemetry_failure_count = 0
        
        self.dedupe_keys_loaded = 0
        self.dedupe_load_duration_ms = 0.0
        self.malformed_lines_skipped = 0
        
        self._load_existing_telemetry_keys()
        
        self.last_tick_bricks_created = 0
        self.last_tick_jump_points = 0.0
        self.last_tick_capped = False
        self.last_rejection_reason = None
        self.last_reversal_timestamp = None
        self.last_signal_event_id = None
        self.last_signal_at = None
        self.last_signal_reason = None

    def _load_existing_telemetry_keys(self):
        start_t = time.time()
        try:
            date_str = datetime.now(TAIPEI_TZ).strftime("%Y%m%d")
            filepath = os.path.join("data", "telemetry", "renko_bricks", date_str, f"{self.symbol}_bricks.jsonl")
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                            if not isinstance(evt, dict):
                                self.malformed_lines_skipped += 1
                                continue
                            g_id = str(evt.get("generation_id", "1"))
                            ep_id = str(evt.get("single_leg_episode_id", ""))
                            seq = evt.get("brick_sequence")
                            if seq is None or not isinstance(seq, int):
                                self.malformed_lines_skipped += 1
                                continue
                            key = f"{g_id}:{ep_id}:{seq}"
                            self._persisted_brick_keys.add(key)
                            self.dedupe_keys_loaded += 1
                        except Exception:
                            self.malformed_lines_skipped += 1
        except Exception as e:
            logger.warning("[RENKO_DEDUPE_LOAD_WARN] Failed to load existing keys: %s", e)
        finally:
            self.dedupe_load_duration_ms = round((time.time() - start_t) * 1000.0, 3)

    def update_brick_size(self, atr: float, multiplier: float = 0.5, min_floor: float = 2.0):
        pass

    def _determine_position_effect(self, trend: int) -> str:
        if self.position_side == "LONG":
            return "FAVORABLE" if trend == 1 else "ADVERSE"
        else:
            return "FAVORABLE" if trend == -1 else "ADVERSE"

    def _create_and_store_brick(
        self,
        *,
        b_open: float,
        b_close: float,
        trend: int,
        is_reversal: bool,
        bricks_created_this_tick: int,
        input_price: float,
        signal_emitted: bool = False,
        signal_reason: Optional[str] = None,
        signal_event_id: Optional[str] = None
    ) -> RenkoBrickEvent:
        bricks_count_abs = max(int(abs(bricks_created_this_tick)), 1)
        
        self.brick_sequence += 1
        self.total_bricks += 1
        
        created_at_str = datetime.now(TAIPEI_TZ).isoformat()
        effect = self._determine_position_effect(trend)
        
        event = RenkoBrickEvent(
            brick_sequence=self.brick_sequence,
            source_receive_sequence=self.source_receive_sequence,
            created_at=created_at_str,
            open=b_open,
            close=b_close,
            high=max(b_open, b_close),
            low=min(b_open, b_close),
            trend=trend,
            trend_label="UP" if trend == 1 else "DOWN",
            is_reversal=is_reversal,
            brick_size=self.locked_brick_size,
            bricks_created_this_tick=bricks_count_abs,
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
            
            line_str = json.dumps(event.to_dict(), ensure_ascii=False)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(line_str + chr(10))
            
            self._persisted_brick_keys.add(dedupe_key)
        except Exception as e:
            self.telemetry_failure_count += 1
            logger.error("[RENKO_TELEMETRY_ERR] Failed to append brick event: %s", e)

    def add(self, price: float) -> Tuple[int, int, Dict[str, Any]]:
        self.source_receive_sequence += 1
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
        
        # [4A] jump validation applies to ALL states (including initial anchor).
        # Rejected ticks return 0 bricks and mutate NOTHING (anchor/trend/
        # sequence/reversal memory/previous accepted price all untouched).
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
                num_bricks = raw_num  # [4A] no cap-drop — full progression
                
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
                        bricks_created_this_tick=num_bricks,
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
                num_bricks = raw_num  # [4A] no cap-drop — full progression

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
                        bricks_created_this_tick=num_bricks,
                        input_price=price
                    )
                    meta["brick_events"].append(event.to_dict())

                self.last_tick_bricks_created = num_bricks
                meta["bricks_created"] = num_bricks
                return num_bricks, self.trend, meta

            elif price <= self.renko_open - self.locked_brick_size:
                diff = (self.renko_open - self.locked_brick_size) - price
                raw_num = 1 + int(diff // self.locked_brick_size)
                num_bricks = raw_num  # [4A] no cap-drop — full progression

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
                    bricks_created_this_tick=num_bricks,
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
                        bricks_created_this_tick=num_bricks,
                        input_price=price
                    )
                    meta["brick_events"].append(c_event.to_dict())

                self.last_tick_bricks_created = -num_bricks
                meta["bricks_created"] = -num_bricks
                return -num_bricks, self.trend, meta

        elif self.trend == -1:
            if price <= self.renko_close - self.locked_brick_size:
                diff = self.renko_close - price
                raw_num = int(diff // self.locked_brick_size)
                num_bricks = raw_num  # [4A] no cap-drop — full progression

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
                        bricks_created_this_tick=num_bricks,
                        input_price=price
                    )
                    meta["brick_events"].append(event.to_dict())

                self.last_tick_bricks_created = -num_bricks
                meta["bricks_created"] = -num_bricks
                return -num_bricks, self.trend, meta

            elif price >= self.renko_open + self.locked_brick_size:
                diff = price - (self.renko_open + self.locked_brick_size)
                raw_num = 1 + int(diff // self.locked_brick_size)
                num_bricks = raw_num  # [4A] no cap-drop — full progression

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
                    bricks_created_this_tick=num_bricks,
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
                        bricks_created_this_tick=num_bricks,
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
            "capability_available": True,
            "tracker_initialized": True,
            "symbol": self.symbol,
            "position_side": self.position_side,
            "price_source": self.price_source,
            "episode_id": self.episode_id,
            "trade_id": self.trade_id,
            "episode_ordinal": self.episode_ordinal,
            "initialized_phase": self.initialized_phase,
            "initialized_at": self.initialized_at,
            "anchor_price": self.anchor_price,
            "renko_open": self.renko_open,
            "renko_close": self.renko_close,
            "trend": self.trend,
            "locked_brick_size": self.locked_brick_size,
            "total_bricks": self.total_bricks,
            "brick_sequence": self.brick_sequence,
            "source_receive_sequence": self.source_receive_sequence,
            "generation_id": self.generation_id,
            "max_bricks_per_tick": self.max_bricks_per_tick,
            "max_price_jump_points": self.max_price_jump_points,
            "last_reversal_timestamp": self.last_reversal_timestamp,
            "last_signal_event_id": self.last_signal_event_id,
            "last_signal_at": self.last_signal_at,
            "last_signal_reason": self.last_signal_reason,
            "telemetry_failure_count": self.telemetry_failure_count,
            "dedupe_keys_loaded": self.dedupe_keys_loaded,
            "dedupe_load_duration_ms": self.dedupe_load_duration_ms,
            "malformed_lines_skipped": self.malformed_lines_skipped,
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
            episode_ordinal=int(data.get("episode_ordinal", 1)),
            generation_id=str(data.get("generation_id", "1")),
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
        tracker.source_receive_sequence = int(data.get("source_receive_sequence", 0))
        tracker.generation_id = str(data.get("generation_id", "1"))
        tracker.last_reversal_timestamp = data.get("last_reversal_timestamp")
        tracker.last_signal_event_id = data.get("last_signal_event_id")
        tracker.last_signal_at = data.get("last_signal_at")
        tracker.last_signal_reason = data.get("last_signal_reason")
        tracker.telemetry_failure_count = int(data.get("telemetry_failure_count", 0))
        tracker.dedupe_keys_loaded = int(data.get("dedupe_keys_loaded", 0))
        tracker.dedupe_load_duration_ms = float(data.get("dedupe_load_duration_ms", 0.0))
        tracker.malformed_lines_skipped = int(data.get("malformed_lines_skipped", 0))
        
        recent = data.get("recent_bricks", [])
        if isinstance(recent, list):
            for b in recent:
                if isinstance(b, dict):
                    tracker._recent_bricks.append(b)
                    g_id = str(b.get("generation_id", 1))
                    ep_id = str(b.get("single_leg_episode_id", ""))
                    seq = b.get("brick_sequence")
                    if seq is not None and isinstance(seq, int):
                        key = f"{g_id}:{ep_id}:{seq}"
                        tracker._persisted_brick_keys.add(key)

        return tracker
