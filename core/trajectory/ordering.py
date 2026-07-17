# 2026-07-17 Gemini CLI: Implement Deterministic Event Ordering and Evidence Auditing for Phase 4B
from __future__ import annotations

import hashlib
import json
from typing import Sequence, Tuple
from core.trajectory.contracts import (
    TrajectoryEvent,
    OrderedTrajectory,
    EventSource,
    EventType,
)

def make_ordering_key(event: TrajectoryEvent) -> Tuple[int, int, int, int, int, str]:
    """
    Generate the deterministic total ordering key (six-tuple) for a TrajectoryEvent
    as defined in ADR-018.
    """
    # 1. Source priority map
    source_prio = {
        EventSource.EXCHANGE: 10,
        EventSource.SHIOAJI_BROKER: 20,
        EventSource.STRATEGY_ROUTER: 30,
        EventSource.SYSTEM_MONITOR: 40,
    }.get(event.source, 99)

    # 2. Event type priority map
    type_prio = {
        EventType.SESSION_BOUNDARY: 1,
        EventType.MARKET_TICK: 2,
        EventType.BROKER_DISCONNECT: 3,
        EventType.BROKER_ACK: 4,
        EventType.BROKER_FILL: 5,
        EventType.LIFECYCLE_TRANSITION: 6,
        EventType.PROCESS_RESTART: 7,
        EventType.BROKER_RECONNECT: 8,
        EventType.STATE_RECONCILED: 9,
    }.get(event.event_type, 99)

    seq = event.source_sequence if event.source_sequence is not None else 0
    recv = event.receive_time_ns if event.receive_time_ns is not None else 0

    return (
        event.event_time_ns,
        source_prio,
        type_prio,
        seq,
        recv,
        event.event_id,
    )

def order_trajectory_events(
    events: Sequence[TrajectoryEvent],
    policy_version: str = "v1.0.0"
) -> OrderedTrajectory:
    """
    Deduplicates and sorts a list of TrajectoryEvents deterministically.
    Calculates input/output counts, duplicates, late events, and the unique ordering hash.
    """
    input_count = len(events)
    
    # 1. Deduplication (Semantic duplicates: same event_time_ns, event_type, source, and payload payload content)
    unique_events = []
    seen_semantics = set()
    duplicate_count = 0

    for event in events:
        # Canonical representation of payload dict for content comparison
        canonical_payload = json.dumps(event.payload, sort_keys=True, separators=(",", ":"))
        semantic_key = (
            event.event_time_ns,
            event.event_type.value,
            event.source.value,
            canonical_payload,
        )
        if semantic_key in seen_semantics:
            duplicate_count += 1
            continue
        seen_semantics.add(semantic_key)
        unique_events.append(event)

    # 2. Sorting using the deterministic total ordering key (six-tuple)
    sorted_events = sorted(unique_events, key=make_ordering_key)

    # 3. Detect late-arriving events
    # Defined as: an event whose receive_time_ns is smaller than the maximum receive_time_ns seen so far in sorted event_time_ns order.
    late_event_count = 0
    max_recv_seen = -1
    for event in sorted_events:
        recv = event.receive_time_ns if event.receive_time_ns is not None else 0
        if max_recv_seen != -1 and recv < max_recv_seen:
            late_event_count += 1
        if recv > max_recv_seen:
            max_recv_seen = recv

    # 4. Compute unique ordering hash
    h = hashlib.sha256()
    h.update(policy_version.encode("utf-8"))
    for event in sorted_events:
        h.update(event.event_id.encode("utf-8"))
    ordering_hash = f"sha256:{h.hexdigest()}"

    return OrderedTrajectory(
        events=tuple(sorted_events),
        ordering_policy_version=policy_version,
        input_event_count=input_count,
        output_event_count=len(sorted_events),
        duplicate_count=duplicate_count,
        late_event_count=late_event_count,
        ordering_hash=ordering_hash,
    )
