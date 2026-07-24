"""
Unit tests for SEP Research Inbox State Machine & Production Promotion Gate
"""

import json
import pytest
from pathlib import Path
from core.research_inbox import process_inbox_bundle, IngestionState
from core.promotion_gate import evaluate_policy_promotion_gate


def test_inbox_quarantined_missing_ready(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "test.parquet").write_text("data")
    
    state, msg, meta = process_inbox_bundle(staging_dir=staging, datasets_dir=tmp_path / "datasets", quarantine_dir=tmp_path / "quarantine")
    assert state == IngestionState.QUARANTINED
    assert "Missing READY marker" in msg


def test_inbox_registered_success(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "test.parquet").write_text("data")
    (staging / "READY").write_text("OK")
    (staging / "dataset_manifest.json").write_text(json.dumps({"build_id": "ds_20260723"}))

    state, msg, meta = process_inbox_bundle(staging_dir=staging, datasets_dir=tmp_path / "datasets", quarantine_dir=tmp_path / "quarantine")
    assert state == IngestionState.AVAILABLE_FOR_RESEARCH
    assert meta["build_id"] == "ds_20260723"


def test_promotion_gate_approved():
    ok, msg, report = evaluate_policy_promotion_gate(
        policy_name="profit-lock-v2",
        evidence_level="E2",
        confirmation_mean_diff_twd=200.0,
        confirmation_ci_lower_bound_twd=50.0,
        max_dd_degradation_pct=2.0,
        catastrophic_loss_count_increase=0,
        replay_validity_pass=True,
        plateau_pass=True,
        regression_suite_pass=True
    )
    assert ok is True
    assert "PROMOTION APPROVED" in msg


def test_promotion_gate_rejected_low_improvement():
    ok, msg, report = evaluate_policy_promotion_gate(
        policy_name="weak-policy",
        evidence_level="E2",
        confirmation_mean_diff_twd=50.0,  # Below 150 min
        confirmation_ci_lower_bound_twd=-10.0,
        max_dd_degradation_pct=2.0,
        catastrophic_loss_count_increase=0,
        replay_validity_pass=True,
        plateau_pass=True,
        regression_suite_pass=True
    )
    assert ok is False
    assert "Confirmation mean diff >=" in msg
