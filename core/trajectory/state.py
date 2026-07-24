# 2026-07-17 Gemini CLI: Implement immutable state snapshots for replay sandbox
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping

@dataclass(frozen=True)
class LifecycleSnapshot:
    state: str
    armed_timestamp_ns: int | None
    active_timestamp_ns: int | None
    last_transition_reason: str | None
    extra_metadata: Mapping[str, object]

@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: int
    avg_price: float
    unrealized_pnl: float

@dataclass(frozen=True)
class OrderIntentSnapshot:
    intent_id: str
    symbol: str
    side: str
    price: float | None
    qty: int
    intent_type: str
    timestamp_ns: int

@dataclass(frozen=True)
class MarketSnapshot:
    last_prices: Mapping[str, float]
    bid_asks: Mapping[str, tuple[float, float]]  # symbol -> (bid, ask)

@dataclass(frozen=True)
class TimerSnapshot:
    active_timers: Mapping[str, int]  # timer_name -> expiry_ns

@dataclass(frozen=True)
class ReplayState:
    session_id: str
    trade_id: str | None
    lifecycle: LifecycleSnapshot
    positions: tuple[PositionSnapshot, ...]
    pending_intents: tuple[OrderIntentSnapshot, ...]
    market: MarketSnapshot
    timers: TimerSnapshot
    last_event_id: str | None
    event_index: int
