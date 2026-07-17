# 2026-07-17 Gemini CLI: Implement Trajectory Event Validation Logic
from __future__ import annotations

from typing import Any, Mapping
from core.trajectory.contracts import (
    EventType,
    EventOrigin,
    EventAuthority,
    EventCausality,
    EventMutability,
    EventSource
)
from core.trajectory.errors import TrajectorySchemaError, TrajectoryValidationError

REQUIRED_FIELDS = {
    "event_id": str,
    "event_type": str,
    "event_time_ns": int,
    "source": str,
    "session_id": str,
    "origin": str,
    "authority": str,
    "causality": str,
    "mutability": str,
    "payload_schema_version": str,
    "payload": dict,
}

def validate_event_dict(data: Mapping[str, Any]) -> None:
    """
    Validate that the input dictionary contains all required fields with correct types,
    and conforms to the Enum constraints defined in the Trajectory contract.
    """
    # 1. Schema checking (existence and type of basic fields)
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data:
            raise TrajectorySchemaError(f"Missing required field: '{field}'")
        val = data[field]
        if val is None or not isinstance(val, expected_type):
            raise TrajectorySchemaError(
                f"Field '{field}' must be of type {expected_type.__name__}, got {type(val).__name__}"
            )

    # 2. Check source_sequence, receive_time_ns, trade_id, and quality_flags types
    for nullable_int in ["receive_time_ns", "source_sequence"]:
        if nullable_int in data and data[nullable_int] is not None:
            if not isinstance(data[nullable_int], int):
                raise TrajectorySchemaError(f"Field '{nullable_int}' must be an integer or null")
                
    if "trade_id" in data and data["trade_id"] is not None:
        if not isinstance(data["trade_id"], str):
            raise TrajectorySchemaError("Field 'trade_id' must be a string or null")

    if "quality_flags" in data:
        q_flags = data["quality_flags"]
        if not isinstance(q_flags, (list, tuple)):
            raise TrajectorySchemaError("Field 'quality_flags' must be a list or tuple of strings")
        for flag in q_flags:
            if not isinstance(flag, str):
                raise TrajectorySchemaError(f"Quality flag '{flag}' must be a string")

    # 3. Enum validation
    try:
        event_type = EventType(data["event_type"])
    except ValueError:
        raise TrajectoryValidationError(f"Invalid event_type: '{data['event_type']}'")

    try:
        origin = EventOrigin(data["origin"])
    except ValueError:
        raise TrajectoryValidationError(f"Invalid origin: '{data['origin']}'")

    try:
        authority = EventAuthority(data["authority"])
    except ValueError:
        raise TrajectoryValidationError(f"Invalid authority: '{data['authority']}'")

    try:
        causality = EventCausality(data["causality"])
    except ValueError:
        raise TrajectoryValidationError(f"Invalid causality: '{data['causality']}'")

    try:
        mutability = EventMutability(data["mutability"])
    except ValueError:
        raise TrajectoryValidationError(f"Invalid mutability: '{data['mutability']}'")

    try:
        source = EventSource(data["source"])
    except ValueError:
        raise TrajectoryValidationError(f"Invalid source: '{data['source']}'")

    # 4. Authority vs Causality semantic constraints (ADR-017 / ADR-020)
    # Observed and simulated facts cannot share the same authority class.
    # Exogenous events represent external market realities and must come from EXCHANGE or PRODUCTION_ENGINE.
    if causality == EventCausality.EXOGENOUS:
        if authority not in (EventAuthority.EXCHANGE, EventAuthority.PRODUCTION_ENGINE):
            raise TrajectoryValidationError(
                f"Exogenous event '{event_type.value}' has unauthorized authority '{authority.value}'. "
                "Must be EXCHANGE or PRODUCTION_ENGINE."
            )

    # 5. Payload structure validation rules
    payload = data["payload"]
    if event_type == EventType.MARKET_TICK:
        required_keys = ["symbol", "bid_price", "ask_price", "last_price"]
        for key in required_keys:
            if key not in payload:
                raise TrajectoryValidationError(f"MARKET_TICK payload missing required key: '{key}'")
    elif event_type == EventType.BROKER_FILL:
        required_keys = ["order_id", "symbol", "side", "price", "quantity"]
        for key in required_keys:
            if key not in payload:
                raise TrajectoryValidationError(f"BROKER_FILL payload missing required key: '{key}'")
