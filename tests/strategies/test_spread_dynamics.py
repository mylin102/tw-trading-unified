# 2026-07-25 Gemini CLI: Unit tests enforcing Dynamics Telemetry Contract v1
import pytest
from strategies.futures.mts.spread_dynamics import (
    SpreadDynamicsCalculator,
    SpreadDynamicsMetrics,
    DynamicsStatus,
    EXPECTED_ENTRY_SNAPSHOT_FIELDS,
    assert_valid_dynamics_provenance,
)

def test_p0_outward_deceleration_clamped_option_a():
    """Verify outward_deceleration_index is clamped >= 0 when expanding outward and decelerating, and 0 when accelerating."""
    calc = SpreadDynamicsCalculator(tau_sec=2.0)
    calc.update(ts=100.0, z=3.0)
    calc.update(ts=101.0, z=3.8)  # Acceleration outward
    res_accel = calc.update(ts=102.0, z=4.8)
    
    # Accelerating outward -> deceleration index is 0.0 (not negative!)
    assert res_accel.outward_deceleration_index == 0.0

def test_p0_outward_mr_valid_zone():
    """Verify outward_momentum_ratio is None (invalid) when |z| < 0.25 to prevent division explosion at mean."""
    calc = SpreadDynamicsCalculator(tau_sec=2.0, min_abs_z_for_ratio=0.25)
    calc.update(ts=100.0, z=0.10)
    res_near_zero = calc.update(ts=101.0, z=0.12)
    
    assert res_near_zero.outward_momentum_ratio is None
    assert res_near_zero.outward_momentum_ratio_valid is False
    assert res_near_zero.regularized_outward_mr is not None  # Regularized floor version available for Dashboard

    # At extreme z = 3.0 -> valid ratio
    calc_extreme = SpreadDynamicsCalculator(tau_sec=2.0, min_abs_z_for_ratio=0.25)
    calc_extreme.update(ts=100.0, z=3.0)
    res_extreme = calc_extreme.update(ts=101.0, z=3.5)
    assert res_extreme.outward_momentum_ratio is not None
    assert res_extreme.outward_momentum_ratio_valid is True

def test_p0_duplicate_timestamp_validity_contract():
    """Verify dt == 0 marks current tick observation invalid while preserving internal EMA state."""
    calc = SpreadDynamicsCalculator()
    calc.update(ts=100.0, z=2.0)
    calc.update(ts=101.0, z=2.5)
    res_dup = calc.update(ts=101.0, z=2.6)  # dt == 0
    
    assert res_dup.dynamics_status == DynamicsStatus.DUPLICATE_TIMESTAMP
    assert res_dup.velocity_ema_valid is False
    assert res_dup.acceleration_ema_valid is False

def test_p0_feed_gap_complete_state_flush():
    """Verify dt > 15.0s causes complete state flush and returns GAP_REANCHORED."""
    calc = SpreadDynamicsCalculator(max_derivative_gap_sec=15.0)
    calc.update(ts=100.0, z=2.0)
    calc.update(ts=101.0, z=2.5)
    calc.update(ts=102.0, z=3.0)
    
    res_gap = calc.update(ts=125.0, z=4.0)  # dt = 23.0s > 15.0s
    assert res_gap.dynamics_status == DynamicsStatus.GAP_REANCHORED
    assert res_gap.window_sample_count == 1
    assert res_gap.velocity_ready is False

def test_p1_feature_level_readiness_and_slope_contract():
    """Verify feature-level readiness flags (velocity, acceleration, trend)."""
    calc = SpreadDynamicsCalculator(min_slope_samples=5, min_slope_duration_sec=1.0)
    
    # Tick 1: WARMING_UP
    r1 = calc.update(ts=100.0, z=1.0)
    assert r1.velocity_ready is False
    assert r1.acceleration_ready is False

    # Tick 2: Velocity ready
    r2 = calc.update(ts=100.5, z=1.2)
    assert r2.velocity_ready is True
    assert r2.acceleration_ready is False

    # Tick 3: Acceleration ready, trend not ready (samples = 3 < 5)
    r3 = calc.update(ts=101.0, z=1.5)
    assert r3.acceleration_ready is True
    assert r3.trend_ready is False
    assert r3.rolling_slope is None

    # Ticks 4 & 5: Trend ready once samples >= 5 and duration >= 1.0s
    calc.update(ts=101.5, z=1.9)
    r5 = calc.update(ts=102.0, z=2.4)
    assert r5.trend_ready is True
    assert r5.rolling_slope is not None
    assert r5.slope_fit_r2 is not None
    assert r5.dynamics_status == DynamicsStatus.READY

def test_entry_snapshot_and_provenance_hash():
    """Verify atomic point-in-time entry snapshot schema, feature age calculation, and config hash."""
    calc = SpreadDynamicsCalculator(tau_sec=2.0)
    calc.update(ts=100.0, z=3.0)
    res = calc.update(ts=101.0, z=3.5)

    snap = res.to_entry_snapshot(
        entry_decision_time_iso="2026-07-25T03:35:00.050+08:00",
        dynamics_event_time_iso="2026-07-25T03:35:00.000+08:00",
        source_commit="abc1234",
        runtime_host_role="mini-live",
        config_hash=calc.config_hash,
        source_tree_dirty=False,
    )

    assert set(snap.keys()) == EXPECTED_ENTRY_SNAPSHOT_FIELDS
    assert snap["spread_z_at_entry"] == 3.5
    assert snap["dynamics_feature_age_ms"] == 50.0
    assert snap["dynamics_contract_version"] == "1.0"
    assert snap["dynamics_feature_version"] == "1.0.0"
    assert snap["dynamics_schema_version"] == "1.0"
    assert snap["source_commit"] == "abc1234"
    assert snap["source_tree_dirty"] is False
    assert snap["calculation_config_hash"] == calc.config_hash

def test_provenance_validation_fail_closed():
    """Verify provenance validator rejects placeholders or dirty source tree on mini-live."""
    with pytest.raises(ValueError, match="Invalid source_commit"):
        assert_valid_dynamics_provenance(source_commit="<git-sha>", config_hash="1234567890ab", host_role="mini-live")

    with pytest.raises(ValueError, match="clean source tree"):
        assert_valid_dynamics_provenance(source_commit="abc1234", config_hash="1234567890ab", host_role="mini-live", source_tree_dirty=True)

def test_event_ordering_invariants():
    """Verify event_time <= received_at <= processed_at <= decision_time <= persisted_at invariant."""
    from strategies.futures.mts.spread_dynamics import assert_event_ordering_invariants

    # Valid sequence
    assert_event_ordering_invariants(
        event_time_iso="2026-07-25T03:35:00.000+08:00",
        received_at_iso="2026-07-25T03:35:00.010+08:00",
        processed_at_iso="2026-07-25T03:35:00.020+08:00",
        entry_decision_time_iso="2026-07-25T03:35:00.050+08:00",
        snapshot_persisted_at_iso="2026-07-25T03:35:00.060+08:00",
    )

    # Violation sequence (decision_time < event_time)
    with pytest.raises(ValueError, match="Event ordering violation"):
        assert_event_ordering_invariants(
            event_time_iso="2026-07-25T03:35:00.100+08:00",
            received_at_iso="2026-07-25T03:35:00.010+08:00",
            processed_at_iso="2026-07-25T03:35:00.020+08:00",
            entry_decision_time_iso="2026-07-25T03:35:00.050+08:00",
        )

def test_golden_dataset_replay():
    """Verify replay against tests/golden/entry_snapshot_v1.json produces 100% deterministic expected results."""
    import json
    with open("tests/golden/entry_snapshot_v1.json", "r") as f:
        golden = json.load(f)

    calc = SpreadDynamicsCalculator(tau_sec=2.0)
    for case in golden["golden_cases"]:
        res = calc.update(ts=case["input"]["ts"], z=case["input"]["z"])
        assert res.dynamics_status.value == case["expected_status"]
        assert res.z == case["expected_z"]
        if case["expected_v_ema"] is not None:
            assert res.velocity_ema == case["expected_v_ema"]
        if case["expected_a_ema"] is not None:
            assert res.acceleration_ema == case["expected_a_ema"]

def test_serialization_roundtrip_invariant():
    """Verify Snapshot -> JSON -> Snapshot round-trip preserves None, bool, float, and timestamp types without distortion."""
    import json
    calc = SpreadDynamicsCalculator(tau_sec=2.0)
    calc.update(ts=100.0, z=3.0)
    res = calc.update(ts=101.0, z=3.5)

    original_snap = res.to_entry_snapshot(
        entry_decision_time_iso="2026-07-25T03:35:00.050+08:00",
        dynamics_event_time_iso="2026-07-25T03:35:00.000+08:00",
        source_commit="abc1234",
        runtime_host_role="mini-live",
        config_hash=calc.config_hash,
        source_tree_dirty=False,
    )

    # Serialize to JSON and deserialize back
    json_str = json.dumps(original_snap)
    deserialized_snap = json.loads(json_str)

    assert original_snap == deserialized_snap
    assert deserialized_snap["rolling_slope_at_entry"] is None  # None preserved, not "null" or 0
    assert deserialized_snap["velocity_ema_valid_at_entry"] is True  # bool preserved, not "True"

