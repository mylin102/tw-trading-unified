# 2026-07-17 Gemini CLI: Unit tests for Trajectory Loader, Schema Validation, and Manifest Verification
from __future__ import annotations

import json
import tempfile
from pathlib import Path
import pytest
import pandas as pd

from core.trajectory.contracts import (
    EventType,
    EventOrigin,
    EventAuthority,
    EventCausality,
    EventMutability,
    EventSource,
    TrajectoryEvent,
)
from core.trajectory.errors import (
    TrajectorySchemaError,
    TrajectoryValidationError,
    DuplicateEventError,
    ManifestVerificationError,
    ReferenceIntegrityError,
)
from core.trajectory.loader import TrajectoryLoader


@pytest.fixture
def valid_event_dict() -> dict:
    return {
        "event_id": "evt-001",
        "event_type": "MARKET_TICK",
        "event_time_ns": 1784268000000000000,
        "receive_time_ns": 1784268000005000000,
        "source_sequence": 12345,
        "source": "exchange",
        "session_id": "s-20260717-day",
        "trade_id": "t-001",
        "origin": "OBSERVED",
        "authority": "EXCHANGE",
        "causality": "EXOGENOUS",
        "mutability": "IMMUTABLE",
        "payload_schema_version": "v1.0.0",
        "payload": {
            "symbol": "TMF_NEAR",
            "bid_price": 14250.0,
            "ask_price": 14252.0,
            "last_price": 14251.0,
        },
        "quality_flags": ["OK"],
    }


def test_valid_jsonl_load(valid_event_dict):
    # Lifecycle transition initialization to satisfy trade reference integrity
    init_event = {
        **valid_event_dict,
        "event_id": "evt-init",
        "event_type": "LIFECYCLE_TRANSITION",
        "authority": "PRODUCTION_ENGINE",
        "causality": "ENDOGENOUS",
        "mutability": "REPLACEABLE",
    }
    
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp:
        tmp.write(json.dumps(init_event) + "\n")
        tmp.write(json.dumps(valid_event_dict) + "\n")
        tmp_path = Path(tmp.name)

    try:
        events = TrajectoryLoader.load_from_jsonl(tmp_path)
        assert len(events) == 2
        assert isinstance(events, tuple)
        assert isinstance(events[0], TrajectoryEvent)
        assert events[0].event_id == "evt-init"
        assert events[1].event_id == "evt-001"
        assert events[1].event_type == EventType.MARKET_TICK
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_missing_required_field_fails(valid_event_dict):
    del valid_event_dict["event_id"]
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp:
        tmp.write(json.dumps(valid_event_dict) + "\n")
        tmp_path = Path(tmp.name)

    try:
        with pytest.raises(TrajectorySchemaError) as exc_info:
            TrajectoryLoader.load_from_jsonl(tmp_path)
        assert "Missing required field" in str(exc_info.value)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_invalid_enum_fails(valid_event_dict):
    valid_event_dict["event_type"] = "INVALID_TYPE"
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp:
        tmp.write(json.dumps(valid_event_dict) + "\n")
        tmp_path = Path(tmp.name)

    try:
        with pytest.raises(TrajectoryValidationError) as exc_info:
            TrajectoryLoader.load_from_jsonl(tmp_path)
        assert "Invalid event_type" in str(exc_info.value)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_causality_authority_constraint_fails(valid_event_dict):
    # Exogenous event cannot have BROKER authority (must be EXCHANGE or PRODUCTION_ENGINE)
    valid_event_dict["authority"] = "BROKER"
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp:
        tmp.write(json.dumps(valid_event_dict) + "\n")
        tmp_path = Path(tmp.name)

    try:
        with pytest.raises(TrajectoryValidationError) as exc_info:
            TrajectoryLoader.load_from_jsonl(tmp_path)
        assert "has unauthorized authority" in str(exc_info.value)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_duplicate_event_id_fails(valid_event_dict):
    init_event = {
        **valid_event_dict,
        "event_id": "evt-001",  # Same ID
        "event_type": "LIFECYCLE_TRANSITION",
        "authority": "PRODUCTION_ENGINE",
        "causality": "ENDOGENOUS",
        "mutability": "REPLACEABLE",
    }
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp:
        tmp.write(json.dumps(init_event) + "\n")
        tmp.write(json.dumps(valid_event_dict) + "\n")
        tmp_path = Path(tmp.name)

    try:
        with pytest.raises(DuplicateEventError) as exc_info:
            TrajectoryLoader.load_from_jsonl(tmp_path)
        assert "Duplicate event_id" in str(exc_info.value)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_reference_integrity_missing_session_fails(valid_event_dict):
    valid_event_dict["session_id"] = "   "  # Empty session ID
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp:
        tmp.write(json.dumps(valid_event_dict) + "\n")
        tmp_path = Path(tmp.name)

    try:
        with pytest.raises(ReferenceIntegrityError) as exc_info:
            TrajectoryLoader.load_from_jsonl(tmp_path)
        assert "empty or missing session_id" in str(exc_info.value)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_reference_integrity_dangling_trade_id_fails(valid_event_dict):
    # valid_event_dict has trade_id="t-001", but no lifecycle transition setup event
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp:
        tmp.write(json.dumps(valid_event_dict) + "\n")
        tmp_path = Path(tmp.name)

    try:
        with pytest.raises(ReferenceIntegrityError) as exc_info:
            TrajectoryLoader.load_from_jsonl(tmp_path)
        assert "have no corresponding lifecycle transition initialization" in str(exc_info.value)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_manifest_verification_pass_and_fail():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dir_path = Path(tmp_dir)
        source_file = dir_path / "test_file.jsonl"
        source_file.write_text("dummy content")

        import hashlib
        h = hashlib.sha256()
        h.update(b"dummy content")
        expected_sha = h.hexdigest()

        manifest_data = {
            "dataset_build_id": "test_build",
            "source_files": [
                {
                    "path": "test_file.jsonl",
                    "sha256": expected_sha
                }
            ]
        }
        manifest_path = dir_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_data))

        # 1. Verification passes
        TrajectoryLoader.verify_manifest(manifest_path, dir_path)

        # 2. Verification fails on hash mismatch
        manifest_data["source_files"][0]["sha256"] = "wrong_hash"
        manifest_path.write_text(json.dumps(manifest_data))
        with pytest.raises(ManifestVerificationError) as exc_info:
            TrajectoryLoader.verify_manifest(manifest_path, dir_path)
        assert "Content hash mismatch" in str(exc_info.value)


def test_valid_parquet_load(valid_event_dict):
    init_event = {
        **valid_event_dict,
        "event_id": "evt-init",
        "event_type": "LIFECYCLE_TRANSITION",
        "authority": "PRODUCTION_ENGINE",
        "causality": "ENDOGENOUS",
        "mutability": "REPLACEABLE",
    }
    
    # Create pandas dataframe
    # payload is serialized as JSON string or dict depending on storage
    init_event["payload"] = json.dumps(init_event["payload"])
    valid_event_dict["payload"] = json.dumps(valid_event_dict["payload"])
    
    df = pd.DataFrame([init_event, valid_event_dict])
    
    with tempfile.NamedTemporaryFile("w+", suffix=".parquet", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        df.to_parquet(tmp_path)

    try:
        events = TrajectoryLoader.load_from_parquet(tmp_path)
        assert len(events) == 2
        assert isinstance(events, tuple)
        assert events[0].event_id == "evt-init"
        assert events[1].event_id == "evt-001"
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def test_deterministic_ordering_truth_table(valid_event_dict):
    from core.trajectory.ordering import order_trajectory_events
    
    # Create several events at the exact same timestamp with different types
    base_time = 1784268000000000000
    
    types_to_create = [
        (EventType.PROCESS_RESTART, "system_monitor", EventAuthority.PRODUCTION_ENGINE, EventCausality.EXOGENOUS, EventMutability.IMMUTABLE),
        (EventType.LIFECYCLE_TRANSITION, "strategy_router", EventAuthority.PRODUCTION_ENGINE, EventCausality.ENDOGENOUS, EventMutability.REPLACEABLE),
        (EventType.BROKER_FILL, "shioaji_broker", EventAuthority.BROKER, EventCausality.ENDOGENOUS, EventMutability.REPLACEABLE),
        (EventType.BROKER_ACK, "shioaji_broker", EventAuthority.BROKER, EventCausality.ENDOGENOUS, EventMutability.REPLACEABLE),
        (EventType.MARKET_TICK, "exchange", EventAuthority.EXCHANGE, EventCausality.EXOGENOUS, EventMutability.IMMUTABLE),
        (EventType.SESSION_BOUNDARY, "exchange", EventAuthority.EXCHANGE, EventCausality.EXOGENOUS, EventMutability.IMMUTABLE),
    ]
    
    raw_events = []
    for idx, (etype, src, auth, caus, mut) in enumerate(types_to_create):
        evt = TrajectoryEvent(
            event_id=f"evt-{idx}",
            event_type=etype,
            event_time_ns=base_time,
            receive_time_ns=base_time + 1000,
            source_sequence=0,
            source=EventSource(src),
            session_id="session-1",
            trade_id=None,
            origin=EventOrigin.OBSERVED,
            authority=auth,
            causality=caus,
            mutability=mut,
            payload_schema_version="v1.0.0",
            payload={},
            quality_flags=(),
        )
        raw_events.append(evt)
        
    # Order them
    ordered_trajectory = order_trajectory_events(raw_events)
    sorted_types = [evt.event_type for evt in ordered_trajectory.events]
    
    # Expected order based on ADR-018 type_prio:
    # 1. SESSION_BOUNDARY
    # 2. MARKET_TICK
    # 3. BROKER_ACK (disconnect is 3, ack is 4)
    # 4. BROKER_FILL (5)
    # 5. LIFECYCLE_TRANSITION (6)
    # 6. PROCESS_RESTART (7)
    expected_order = [
        EventType.SESSION_BOUNDARY,
        EventType.MARKET_TICK,
        EventType.BROKER_ACK,
        EventType.BROKER_FILL,
        EventType.LIFECYCLE_TRANSITION,
        EventType.PROCESS_RESTART,
    ]
    assert sorted_types == expected_order


def test_deterministic_ordering_duplicates_and_late_events(valid_event_dict):
    from core.trajectory.ordering import order_trajectory_events
    
    base_evt = TrajectoryEvent(
        event_id="evt-1",
        event_type=EventType.MARKET_TICK,
        event_time_ns=1000,
        receive_time_ns=2000,
        source_sequence=1,
        source=EventSource.EXCHANGE,
        session_id="session-1",
        trade_id=None,
        origin=EventOrigin.OBSERVED,
        authority=EventAuthority.EXCHANGE,
        causality=EventCausality.EXOGENOUS,
        mutability=EventMutability.IMMUTABLE,
        payload_schema_version="v1.0.0",
        payload={"symbol": "TMF"},
        quality_flags=(),
    )
    
    # Duplicate semantic event (same time, type, source, payload but different ID)
    dup_evt = TrajectoryEvent(
        event_id="evt-2",
        event_type=EventType.MARKET_TICK,
        event_time_ns=1000,
        receive_time_ns=2050,
        source_sequence=2,
        source=EventSource.EXCHANGE,
        session_id="session-1",
        trade_id=None,
        origin=EventOrigin.OBSERVED,
        authority=EventAuthority.EXCHANGE,
        causality=EventCausality.EXOGENOUS,
        mutability=EventMutability.IMMUTABLE,
        payload_schema_version="v1.0.0",
        payload={"symbol": "TMF"},
        quality_flags=(),
    )
    
    # Late arriving event (event_time_ns=500, but receive_time_ns=3000)
    late_evt = TrajectoryEvent(
        event_id="evt-3",
        event_type=EventType.MARKET_TICK,
        event_time_ns=500,
        receive_time_ns=3000,
        source_sequence=3,
        source=EventSource.EXCHANGE,
        session_id="session-1",
        trade_id=None,
        origin=EventOrigin.OBSERVED,
        authority=EventAuthority.EXCHANGE,
        causality=EventCausality.EXOGENOUS,
        mutability=EventMutability.IMMUTABLE,
        payload_schema_version="v1.0.0",
        payload={"symbol": "TMF_OTHER"},
        quality_flags=(),
    )
    
    ordered_trajectory = order_trajectory_events([base_evt, dup_evt, late_evt])
    
    assert ordered_trajectory.input_event_count == 3
    assert ordered_trajectory.output_event_count == 2
    assert ordered_trajectory.duplicate_count == 1
    assert ordered_trajectory.late_event_count == 1
    # Sorted order should be late_evt (time=500) then base_evt (time=1000)
    assert ordered_trajectory.events[0].event_id == "evt-3"
    assert ordered_trajectory.events[1].event_id == "evt-1"


def test_ordering_hash_stability(valid_event_dict):
    from core.trajectory.ordering import order_trajectory_events
    import random
    
    # Create 5 distinct events
    events = []
    for i in range(5):
        evt = TrajectoryEvent(
            event_id=f"evt-{i}",
            event_type=EventType.MARKET_TICK,
            event_time_ns=i * 100,
            receive_time_ns=i * 100 + 5,
            source_sequence=i,
            source=EventSource.EXCHANGE,
            session_id="session-1",
            trade_id=None,
            origin=EventOrigin.OBSERVED,
            authority=EventAuthority.EXCHANGE,
            causality=EventCausality.EXOGENOUS,
            mutability=EventMutability.IMMUTABLE,
            payload_schema_version="v1.0.0",
            payload={"symbol": f"TMF-{i}"},
            quality_flags=(),
        )
        events.append(evt)
        
    # Replay 100 times with randomized input permutations
    hashes = set()
    for _ in range(100):
        shuffled = list(events)
        random.shuffle(shuffled)
        res = order_trajectory_events(shuffled)
        hashes.add(res.ordering_hash)
        
    assert len(hashes) == 1  # All permutations yield identical ordering hash

