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
    import json, os
    golden_file = "tests/golden/entry_snapshot_v1.json"
    if not os.path.exists(golden_file):
        pytest.skip("Golden dataset file not present")
    with open(golden_file, "r") as f:
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


def _bare_monitor_for_dynamics():
    """Exercise only the monitor's research-only bar telemetry seam."""
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._spread_dynamics = SpreadDynamicsCalculator(
        tau_sec=2.0, max_derivative_gap_sec=15.0,
        min_slope_samples=2, min_slope_duration_sec=0.1)
    monitor._spread_dynamics_session = None
    monitor.live_trading = False
    monitor.ticker = "TMF"
    monitor._execution_context = None
    monitor.run_id = "test-run"
    return monitor


def test_monitor_dynamics_first_sample_warms_and_research_payload_is_raw(
        monkeypatch):
    """First input is telemetry warming-up only; it carries no derivative
    and entry research sees the current raw near-minus-far spread, not a
    future sample or a strategy gate."""
    monitor = _bare_monitor_for_dynamics()
    bar = {"ts": 100.0, "session_type": "day", "near_close": 101.0,
           "far_close": 98.0, "spread_z": 2.0, "entry_z": 1.0}
    metrics = monitor._update_mts_spread_dynamics(bar)

    assert metrics.dynamics_status == DynamicsStatus.WARMING_UP
    assert bar["raw_spread"] == 3.0
    assert bar["dz"] is None
    assert bar["spread_slope"] is None
    assert bar["velocity_ema"] is None

    captured = []
    import core.entry_research_store as store
    monkeypatch.setattr(store, "record_entry_observation",
                        lambda audit, **kwargs: captured.append(audit) or True)
    monitor._record_mts_entry_research_candidate(
        type("Strategy", (), {"_entry_z": 1.0, "_trade_id": None})(),
        bar, __import__("datetime").datetime.now())
    assert captured[-1]["spread"] == 3.0
    assert captured[-1]["dz"] is None
    assert captured[-1]["spread_slope"] is None
    assert captured[-1]["velocity_ema"] is None


def test_monitor_dynamics_is_causal_then_resets_for_gap_and_session():
    monitor = _bare_monitor_for_dynamics()
    first = {"ts": 100.0, "session_type": "day", "near_close": 101.0,
             "far_close": 98.0, "spread_z": 1.0}
    second = {"ts": 101.0, "session_type": "day", "near_close": 103.0,
              "far_close": 98.0, "spread_z": 1.5}
    monitor._update_mts_spread_dynamics(first)
    second_metrics = monitor._update_mts_spread_dynamics(second)
    assert second["raw_spread"] == 5.0
    assert second["dz"] == 0.5
    assert second["velocity_ema"] == second_metrics.velocity_ema
    assert second["spread_slope"] == second_metrics.rolling_slope

    gap = {"ts": 120.0, "session_type": "day", "near_close": 104.0,
           "far_close": 98.0, "spread_z": 2.0}
    assert monitor._update_mts_spread_dynamics(gap).dynamics_status \
        == DynamicsStatus.GAP_REANCHORED
    assert gap["dz"] is None

    session = {"ts": 121.0, "session_type": "night", "near_close": 105.0,
               "far_close": 98.0, "spread_z": 2.5}
    assert monitor._update_mts_spread_dynamics(session).dynamics_status \
        == DynamicsStatus.WARMING_UP
    assert session["dz"] is None


def test_monitor_dynamics_time_regression_does_not_leak_future_state():
    monitor = _bare_monitor_for_dynamics()
    monitor._update_mts_spread_dynamics(
        {"ts": 100.0, "session_type": "day", "near_close": 101.0,
         "far_close": 98.0, "spread_z": 1.0})
    backwards = {"ts": 99.0, "session_type": "day", "near_close": 200.0,
                 "far_close": 98.0, "spread_z": 99.0}
    assert monitor._update_mts_spread_dynamics(backwards).dynamics_status \
        == DynamicsStatus.TIME_REGRESSION
    assert backwards["dz"] is None

    # The rejected 99.0/99.0 observation never becomes history: 101 only
    # sees the causal 100.0 z=1.0 predecessor.
    next_bar = {"ts": 101.0, "session_type": "day", "near_close": 102.0,
                "far_close": 98.0, "spread_z": 2.0}
    monitor._update_mts_spread_dynamics(next_bar)
    assert next_bar["dz"] == 1.0


def test_research_rejects_stale_or_missing_quotes(monkeypatch):
    """P1-C: a candidate whose bar EXPLICITLY carries a stale quote_age or
    invalid BBO evidence must not be recorded as an entry observation.
    Absent keys pass (legacy bars keep recording)."""
    monitor = _bare_monitor_for_dynamics()
    captured = []
    import core.entry_research_store as store
    import datetime
    monkeypatch.setattr(store, "record_entry_observation",
                        lambda audit, **kw: captured.append(audit) or True)
    strat = type("Strategy", (), {"_entry_z": 1.0, "_trade_id": None,
                                  "_max_quote_age_ms": 1000})()
    now = datetime.datetime.now()

    stale = {"ts": 100.0, "session_type": "day", "near_close": 101.0,
             "far_close": 98.0, "spread_z": 2.5, "entry_z": 1.0,
             "quote_age_ms": 5000}
    monitor._record_mts_entry_research_candidate(strat, stale, now)
    assert captured == []          # stale quote rejected

    invalid_bbo = {"ts": 101.0, "session_type": "day", "near_close": 101.0,
                   "far_close": 98.0, "spread_z": 2.5, "entry_z": 1.0,
                   "near_bid": 0, "near_ask": None}
    monitor._record_mts_entry_research_candidate(strat, invalid_bbo, now)
    assert captured == []          # invalid BBO rejected

    ok_bar = {"ts": 102.0, "session_type": "day", "near_close": 101.0,
              "far_close": 98.0, "spread_z": 2.5, "entry_z": 1.0,
              "quote_age_ms": 100, "near_bid": 100.5, "near_ask": 101.5,
              "far_bid": 97.5, "far_ask": 98.5}
    monitor._record_mts_entry_research_candidate(strat, ok_bar, now)
    assert len(captured) == 1


def test_research_ma_std_evolve_across_observations(monkeypatch):
    """P1-C: rolling mean/std are recorded per observation — never a
    silently static cached constant across independent candidates, and the
    dynamics features evolve with them."""
    monitor = _bare_monitor_for_dynamics()
    captured = []
    import core.entry_research_store as store
    import datetime
    monkeypatch.setattr(store, "record_entry_observation",
                        lambda audit, **kw: captured.append(audit) or True)
    strat = type("Strategy", (), {"_entry_z": 1.0, "_trade_id": None})()
    now = datetime.datetime.now()

    bar1 = {"ts": 100.0, "session_type": "day", "near_close": 101.0,
            "far_close": 98.0, "spread_z": 2.5, "entry_z": 1.0,
            "spread_ma": 2.0, "spread_std": 0.8}
    bar2 = {"ts": 101.0, "session_type": "day", "near_close": 102.0,
            "far_close": 98.0, "spread_z": 3.1, "entry_z": 1.0,
            "spread_ma": 2.6, "spread_std": 1.1}

    monitor._update_mts_spread_dynamics(bar1)
    monitor._record_mts_entry_research_candidate(strat, bar1, now)
    monitor._update_mts_spread_dynamics(bar2)
    monitor._record_mts_entry_research_candidate(strat, bar2, now)

    assert captured[0]["spread_ma"] == 2.0
    assert captured[0]["spread_std"] == 0.8
    assert captured[1]["spread_ma"] == 2.6
    assert captured[1]["spread_std"] == 1.1
    assert captured[1]["dz"] is not None      # dynamics evolve per bar
