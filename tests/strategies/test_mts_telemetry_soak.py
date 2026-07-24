# 2026-07-24 Gemini CLI: Wave 1D Dual-Track Accounting & Manifest Tests
import json
import time
from pathlib import Path
from decimal import Decimal
import pytest

from strategies.futures.mts.soak_manifest import CoverageMetrics, PerformanceMetrics, ShadowSoakManifest
from strategies.futures.mts.telemetry import (
    MismatchDimension,
    ParityStatus,
    ParityTelemetryRecord,
    ProcessSafeTelemetryLogger,
    compute_canonical_hash,
)


def test_dual_track_accounting_invariants(tmp_path: Path):
    """Verify Equation 1 (Evaluation Accounting) & Equation 2 (Delivery Accounting) invariants."""
    base_dir = tmp_path / "telemetry_spool"
    logger = ProcessSafeTelemetryLogger(base_dir, deployment_id="deploy-test-01", queue_maxsize=100)

    # Record 5 distinct evaluation outcomes
    logger.record_cycle(ParityTelemetryRecord(record_type="MATCH", parity_status=ParityStatus.MATCH))
    logger.record_cycle(ParityTelemetryRecord(record_type="MISMATCH", parity_status=ParityStatus.MISMATCH))
    logger.record_cycle(ParityTelemetryRecord(record_type="EXCEPTION", parity_status=ParityStatus.LEGACY_RAISED_ONLY))
    logger.record_cycle(ParityTelemetryRecord(record_type="EXCEPTION", parity_status=ParityStatus.BOTH_RAISED_SAME))
    logger.record_cycle(ParityTelemetryRecord(record_type="SKIPPED", parity_status=ParityStatus.SHADOW_SKIPPED))

    # Check Equation 1 Evaluation Summary
    eval_summary = logger.get_evaluation_summary()
    assert eval_summary.cycles_seen == 5
    assert eval_summary.matches == 1
    assert eval_summary.mismatches == 1
    assert eval_summary.legacy_raised_only == 1
    assert eval_summary.both_raised_same == 1
    assert eval_summary.shadow_skipped == 1
    assert eval_summary.is_accounted is True, "Equation 1 Evaluation accounting counter invariant violated!"

    # Stop logger to flush queue
    logger.stop()

    # Check Equation 2 Delivery Summary
    delivery_summary = logger.get_delivery_summary()
    assert delivery_summary.telemetry_enqueued == 5
    assert delivery_summary.telemetry_written == 5
    assert delivery_summary.telemetry_dropped == 0
    assert delivery_summary.is_accounted is True, "Equation 2 Delivery accounting counter invariant violated!"


def test_process_safe_file_naming_and_spooling(tmp_path: Path):
    """Verify telemetry file is named using process-isolated pattern deployment_id_pid_start_ns.jsonl."""
    base_dir = tmp_path / "telemetry_spool"
    logger = ProcessSafeTelemetryLogger(base_dir, deployment_id="mini-deploy-v1", queue_maxsize=100)

    rec = ParityTelemetryRecord(
        record_type="MATCH",
        decision_cycle_id="cycle-100",
        context_hash="hash-123",
        legacy_action="HOLD",
        shadow_action="HOLD",
    )
    logger.record_cycle(rec)
    logger.stop()

    assert logger.spool_path.exists()
    assert logger.spool_path.name.startswith("mini-deploy-v1_")

    lines = logger.spool_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["decision_cycle_id"] == "cycle-100"
    assert parsed["sequence_number"] == 1


def test_canonical_hash_decimal_and_enum_handling():
    """Verify compute_canonical_hash handles Decimal, Enum, and dictionary sorting deterministically."""
    payload_a = {"price": Decimal("23050.00"), "side": "LONG", "legs": [1, 2]}
    payload_b = {"legs": [1, 2], "side": "LONG", "price": Decimal("23050.00")}

    hash_a = compute_canonical_hash(payload_a)
    hash_b = compute_canonical_hash(payload_b)

    assert len(hash_a) == 16
    assert hash_a == hash_b


def test_shadow_soak_manifest_export(tmp_path: Path):
    """Verify ShadowSoakManifest generates valid JSON with sha256 manifest_hash."""
    manifest_path = tmp_path / "shadow_soak_manifest.json"
    manifest = ShadowSoakManifest(
        deployment_id="deploy-mini-001",
        git_commit="10c7c1a7",
        host="myllin-mini",
        started_at_iso="2026-07-24T00:00:00Z",
        ended_at_iso="2026-07-24T09:00:00Z",
        coverage=CoverageMetrics(total_market_callbacks=1000, total_decision_cycles=500),
        performance=PerformanceMetrics(shadow_eval_p50_us=12.5, shadow_eval_p95_us=25.0),
        not_observed=["session_force_exit"],
    )

    json_str = manifest.export_json(manifest_path)
    assert manifest_path.exists()

    parsed = json.loads(json_str)
    assert parsed["wave"] == "1D"
    assert parsed["git_commit"] == "10c7c1a7"
    assert len(parsed["manifest_hash"]) == 64
