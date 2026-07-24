# 2026-07-24 Gemini CLI: Wave 1D Shadow Soak & Telemetry Validation Tests
import json
import time
from pathlib import Path
from decimal import Decimal
import pytest

from strategies.futures.mts.config import NormalReleaseConfig
from strategies.futures.mts.context_builder import SpreadContextBuilder
from strategies.futures.mts.contracts import ExitAction, ExitReason, Leg, Side
from strategies.futures.mts.dispatcher import NormalReleaseDispatcher
from strategies.futures.mts.legacy_adapter import LegacyReleaseAdapter
from strategies.futures.mts.normal_release_policy import NormalReleasePolicy
from strategies.futures.mts.state import NormalReleaseState
from strategies.futures.mts.telemetry import (
    MismatchDimension,
    ParityStatus,
    ParityTelemetryRecord,
    ShadowTelemetryLogger,
    compute_payload_hash,
)
from test_mts_characterization import _build_mock_context


def test_telemetry_accounting_counter_invariant(tmp_path: Path):
    """Verify accounting counter invariant: cycles_seen == matches + mismatches + legacy_raised + policy_raised + shadow_skipped."""
    spool_file = tmp_path / "mts_parity_spool.jsonl"
    logger = ShadowTelemetryLogger(spool_file, queue_maxsize=100)

    # 1. Record MATCH
    logger.record_cycle(ParityTelemetryRecord(record_type="MATCH", parity_status=ParityStatus.MATCH))
    # 2. Record MISMATCH
    logger.record_cycle(ParityTelemetryRecord(record_type="MISMATCH", parity_status=ParityStatus.MISMATCH))
    # 3. Record LEGACY_RAISED
    logger.record_cycle(ParityTelemetryRecord(record_type="EXCEPTION", parity_status=ParityStatus.LEGACY_RAISED))
    # 4. Record SHADOW_SKIPPED
    logger.record_cycle(ParityTelemetryRecord(record_type="SKIPPED", parity_status=ParityStatus.SHADOW_SKIPPED))

    summary = logger.get_summary()

    assert summary.cycles_seen == 4
    assert summary.matches == 1
    assert summary.mismatches == 1
    assert summary.legacy_raised == 1
    assert summary.shadow_skipped == 1
    assert summary.is_accounted is True, "Denominator accounting counter invariant violated!"

    logger.stop()


def test_telemetry_bounded_queue_overflow(tmp_path: Path):
    """Verify non-blocking logger drops overflow records and increments drop counter without throwing exception."""
    spool_file = tmp_path / "overflow_spool.jsonl"
    # Small queue to force overflow
    logger = ShadowTelemetryLogger(spool_file, queue_maxsize=5)

    # Fast fill beyond capacity
    for i in range(50):
        logger.record_cycle(ParityTelemetryRecord(record_type="MATCH", event_id=f"evt-{i}"))

    summary = logger.get_summary()

    assert summary.cycles_seen == 50
    assert summary.telemetry_dropped > 0, "Overflow records should increment telemetry_dropped"

    logger.stop()


def test_jsonl_spool_format_and_sanitization(tmp_path: Path):
    """Verify JSONL spool output file contains valid canonical JSON without credentials or secret leaks."""
    spool_file = tmp_path / "sanitized_spool.jsonl"
    logger = ShadowTelemetryLogger(spool_file, queue_maxsize=100)

    rec = ParityTelemetryRecord(
        record_type="MATCH",
        decision_cycle_id="cycle-999",
        context_hash="abc123hash",
        legacy_action="HOLD",
        shadow_action="HOLD",
        details={"clean_field": "val123"},
    )
    logger.record_cycle(rec)
    logger.stop()

    lines = spool_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1

    parsed = json.loads(lines[0])
    assert parsed["decision_cycle_id"] == "cycle-999"
    assert parsed["context_hash"] == "abc123hash"
    # Verify zero secret keys leak in payload
    raw_str = lines[0].lower()
    assert "password" not in raw_str
    assert "secret" not in raw_str
    assert "token" not in raw_str


def test_payload_hash_computation():
    """Verify sha256 payload hash helper produces deterministic 16-character hex string."""
    payload_a = {"action": "RELEASE", "leg": "NEAR", "reason": "TRIGGERED"}
    payload_b = {"action": "RELEASE", "leg": "NEAR", "reason": "TRIGGERED"}
    payload_c = {"action": "HOLD", "leg": None, "reason": "NONE"}

    hash_a = compute_payload_hash(payload_a)
    hash_b = compute_payload_hash(payload_b)
    hash_c = compute_payload_hash(payload_c)

    assert len(hash_a) == 16
    assert hash_a == hash_b
    assert hash_a != hash_c
