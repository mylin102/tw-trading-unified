# 2026-07-17 Gemini CLI: Implement Replay Trace Frame, Trace, and Canonical Hash
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Mapping, Sequence, Any

from core.trajectory.contracts import EventType
from core.trajectory.state import ReplayState, OrderIntentSnapshot

class EventConsumptionMode(str, Enum):
    INPUT = "INPUT"
    ORACLE_ONLY = "ORACLE_ONLY"
    INPUT_AND_ORACLE = "INPUT_AND_ORACLE"

@dataclass(frozen=True)
class DecisionSnapshot:
    action: str
    symbol: str
    price: float | None
    qty: int
    reason: str
    timestamp_ns: int

@dataclass(frozen=True)
class TransitionSnapshot:
    state_before: str
    state_after: str
    reason: str
    timestamp_ns: int

@dataclass(frozen=True)
class ReplayTraceFrame:
    event_index: int
    event_id: str
    event_type: EventType
    state_before_hash: str
    state_after_hash: str
    decision: DecisionSnapshot | None
    order_intents: tuple[OrderIntentSnapshot, ...]
    transitions: tuple[TransitionSnapshot, ...]
    consumed_as: EventConsumptionMode
    diagnostics: tuple[str, ...]

@dataclass(frozen=True)
class BootstrapMetadata:
    mode: str
    initial_timestamp_ns: int
    provenance: Mapping[str, object]

@dataclass(frozen=True)
class BaselineReplayTrace:
    bootstrap: BootstrapMetadata
    ordering_hash: str
    frames: tuple[ReplayTraceFrame, ...]
    final_state: ReplayState
    canonical_trace_hash: str

def compute_canonical_trace_hash(
    frames: Sequence[ReplayTraceFrame],
    final_state: ReplayState
) -> str:
    """
    Compute a deterministic SHA-256 hash of the replay trace,
    excluding non-deterministic fields like runtime system paths or generated timestamps.
    """
    # Serialize states and frames to basic primitives
    frame_list = []
    for frame in frames:
        frame_list.append({
            "event_index": frame.event_index,
            "event_id": frame.event_id,
            "event_type": frame.event_type.value,
            "state_before_hash": frame.state_before_hash,
            "state_after_hash": frame.state_after_hash,
            "decision": asdict(frame.decision) if frame.decision else None,
            "order_intents": [asdict(i) for i in frame.order_intents],
            "transitions": [asdict(t) for t in frame.transitions],
            "consumed_as": frame.consumed_as.value,
            "diagnostics": list(frame.diagnostics),
        })

    # Normalize float values in state serialization to prevent repr diffs
    def float_normalizer(obj: Any) -> Any:
        if isinstance(obj, float):
            return round(obj, 6)
        if isinstance(obj, dict):
            return {k: float_normalizer(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [float_normalizer(x) for x in obj]
        return obj

    state_dict = float_normalizer(asdict(final_state))

    payload = {
        "frames": frame_list,
        "final_state": state_dict,
    }

    # Format to canonical JSON
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256()
    h.update(serialized.encode("utf-8"))
    return f"sha256:{h.hexdigest()}"
