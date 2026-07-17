# 2026-07-17 Gemini CLI: Define Trajectory Contract DTOs and Enums for Phase 4B
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

class EventType(str, Enum):
    MARKET_TICK = "MARKET_TICK"
    SESSION_BOUNDARY = "SESSION_BOUNDARY"
    BROKER_ACK = "BROKER_ACK"
    BROKER_FILL = "BROKER_FILL"
    LIFECYCLE_TRANSITION = "LIFECYCLE_TRANSITION"
    POSITION_STATE = "POSITION_STATE"
    PROCESS_RESTART = "PROCESS_RESTART"
    BROKER_DISCONNECT = "BROKER_DISCONNECT"
    BROKER_RECONNECT = "BROKER_RECONNECT"
    STATE_RECONCILED = "STATE_RECONCILED"
    VIRTUAL_ORDER_SUBMIT = "VIRTUAL_ORDER_SUBMIT"
    VIRTUAL_FILL = "VIRTUAL_FILL"
    VIRTUAL_LIFECYCLE = "VIRTUAL_LIFECYCLE"

class EventOrigin(str, Enum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    RECONSTRUCTED = "RECONSTRUCTED"
    COUNTERFACTUAL = "COUNTERFACTUAL"

class EventAuthority(str, Enum):
    EXCHANGE = "EXCHANGE"
    BROKER = "BROKER"
    PRODUCTION_ENGINE = "PRODUCTION_ENGINE"
    REPLAY_ENGINE = "REPLAY_ENGINE"

class EventCausality(str, Enum):
    EXOGENOUS = "EXOGENOUS"
    ENDOGENOUS = "ENDOGENOUS"

class EventMutability(str, Enum):
    IMMUTABLE = "IMMUTABLE"
    REPLACEABLE = "REPLACEABLE"

class EventSource(str, Enum):
    EXCHANGE = "exchange"
    SHIOAJI_BROKER = "shioaji_broker"
    STRATEGY_ROUTER = "strategy_router"
    SYSTEM_MONITOR = "system_monitor"

@dataclass(frozen=True)
class TrajectoryEvent:
    event_id: str
    event_type: EventType
    event_time_ns: int
    receive_time_ns: int | None
    source_sequence: int | None
    source: EventSource
    session_id: str
    trade_id: str | None
    origin: EventOrigin
    authority: EventAuthority
    causality: EventCausality
    mutability: EventMutability
    payload_schema_version: str
    payload: Mapping[str, object]
    quality_flags: tuple[str, ...]

@dataclass(frozen=True)
class OrderedTrajectory:
    events: tuple[TrajectoryEvent, ...]
    ordering_policy_version: str
    input_event_count: int
    output_event_count: int
    duplicate_count: int
    late_event_count: int
    ordering_hash: str
