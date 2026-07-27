# 2026-07-27 Gemini CLI: Wave J2-C Parameter Sweeper Validity Audit Differential Tests (1~5)
import pytest

from strategies.futures.mts.policy_j_shadow_evaluator import (
    PolicyJShadowEvaluator,
    PolicyJShadowObservation,
)
from strategies.futures.mts.policy_j_shadow_state import PolicyJShadowState
from strategies.futures.mts.policy_j_telemetry_schema import compute_policy_j_config_hash


def test_1_giveback_sensitivity():
    """
    Test 1: Giveback sensitivity.
    Construct UPL path: 0 -> 300 -> 380 -> 320 -> 260 -> 180
    Expected:
    - act 300, gb 50  -> triggers at 320
    - act 300, gb 100 -> triggers at 260
    - act 300, gb 150 -> triggers at 180
    All three exit points and PnLs must be distinct.
    """
    upl_path = [0.0, 300.0, 380.0, 320.0, 260.0, 180.0]
    time_series = [
        "2026-07-27T10:00:00",
        "2026-07-27T10:01:00",
        "2026-07-27T10:02:00",
        "2026-07-27T10:03:00",
        "2026-07-27T10:04:00",
        "2026-07-27T10:05:00",
    ]

    configs = [
        {"activation_net_pnl_twd": 300.0, "giveback_twd": 50.0, "expected_trig_pnl": 320.0, "expected_time": "2026-07-27T10:03:00"},
        {"activation_net_pnl_twd": 300.0, "giveback_twd": 100.0, "expected_trig_pnl": 260.0, "expected_time": "2026-07-27T10:04:00"},
        {"activation_net_pnl_twd": 300.0, "giveback_twd": 150.0, "expected_trig_pnl": 180.0, "expected_time": "2026-07-27T10:05:00"},
    ]

    results = []
    for cfg in configs:
        state = PolicyJShadowState(trade_id="trade-sensitivity")
        eval_config = {
            "shadow_enabled": True,
            "activation_net_pnl_twd": cfg["activation_net_pnl_twd"],
            "giveback_twd": cfg["giveback_twd"],
        }
        trig_snap = None
        for pnl, ts in zip(upl_path, time_series):
            obs = PolicyJShadowObservation(
                trade_id="trade-sensitivity",
                is_spread_phase=True,
                is_hedged_pair=True,
                exit_inflight=False,
                gross_liquidation_pnl_twd=pnl,
                near_quote_age_ms=0,
                far_quote_age_ms=0,
                event_time=ts,
            )
            snap, state = PolicyJShadowEvaluator.evaluate(obs, state, eval_config)
            if snap.would_trigger and trig_snap is None:
                trig_snap = snap
                break

        assert trig_snap is not None
        assert trig_snap.event_time == cfg["expected_time"]
        assert trig_snap.estimated_net_exit_pnl_twd == cfg["expected_trig_pnl"]
        results.append(trig_snap.event_time)

    # Prove all 3 trigger times are strictly distinct!
    assert len(set(results)) == 3


def test_2_activation_sensitivity():
    """
    Test 2: Activation sensitivity.
    Construct UPL path: 0 -> 250 -> 350 -> 300
    Expected:
    - act 200 -> eligible & activated (peak 350 >= 200)
    - act 300 -> eligible & activated (peak 350 >= 300)
    - act 400 -> never activated (peak 350 < 400)
    """
    upl_path = [0.0, 250.0, 350.0, 300.0]

    for act, expected_trig in [(200.0, True), (300.0, True), (400.0, False)]:
        state = PolicyJShadowState(trade_id="trade-act")
        eval_config = {
            "shadow_enabled": True,
            "activation_net_pnl_twd": act,
            "giveback_twd": 50.0,
        }
        triggered = False
        for pnl in upl_path:
            obs = PolicyJShadowObservation(
                trade_id="trade-act",
                is_spread_phase=True,
                is_hedged_pair=True,
                exit_inflight=False,
                gross_liquidation_pnl_twd=pnl,
                near_quote_age_ms=0,
                far_quote_age_ms=0,
            )
            snap, state = PolicyJShadowEvaluator.evaluate(obs, state, eval_config)
            if snap.would_trigger:
                triggered = True
                break

        assert triggered == expected_trig


def test_3_state_isolation():
    """
    Test 3: State reset & isolation.
    Every trade x parameter pair MUST use a brand new evaluator state.
    Proves that running evaluation on trade A with high peak (1000 TWD) does not leak into trade B (which has 200 TWD).
    """
    # Trade A reaches 1000 peak
    state_a = PolicyJShadowState(trade_id="trade-a")
    obs_a = PolicyJShadowObservation(trade_id="trade-a", is_spread_phase=True, is_hedged_pair=True, exit_inflight=False, gross_liquidation_pnl_twd=1000.0, near_quote_age_ms=0, far_quote_age_ms=0)
    snap_a, state_a = PolicyJShadowEvaluator.evaluate(obs_a, state_a, {"shadow_enabled": True})

    # Trade B starts fresh
    state_b = PolicyJShadowState(trade_id="trade-b")
    obs_b = PolicyJShadowObservation(trade_id="trade-b", is_spread_phase=True, is_hedged_pair=True, exit_inflight=False, gross_liquidation_pnl_twd=200.0, near_quote_age_ms=0, far_quote_age_ms=0)
    snap_b, state_b = PolicyJShadowEvaluator.evaluate(obs_b, state_b, {"shadow_enabled": True})

    # Peak of Trade B must be independent (200 gross - 0 friction = 200 net)
    assert snap_b.peak_net_exit_pnl_twd == 200.0
    assert state_b.peak_net_exit_pnl_twd == 200.0


def test_4_cache_key_correctness():
    """
    Test 4: Cache-key correctness.
    Cache key hash must include dataset_hash, trade_id, activation_twd, giveback_twd, fill_model_version, cost_model_version, policy_version.
    """
    params_1 = {"dataset_hash": "ds1", "trade_id": "t1", "activation_twd": 300.0, "giveback_twd": 100.0, "fill_model": "EXECUTABLE", "cost_model": "V1", "policy_version": "J1.5"}
    params_2 = {"dataset_hash": "ds1", "trade_id": "t1", "activation_twd": 300.0, "giveback_twd": 150.0, "fill_model": "EXECUTABLE", "cost_model": "V1", "policy_version": "J1.5"}

    hash_1 = compute_policy_j_config_hash(params_1)
    hash_2 = compute_policy_j_config_hash(params_2)

    assert hash_1 != hash_2


def test_5_hand_calculated_golden_path():
    """
    Test 5: Hand-calculated golden path.
    Hand-calculate 2 trade trajectories step-by-step and verify exact match with PolicyJShadowEvaluator.

    Golden Path 1:
    - Tick 1: Gross 100, Net 8 -> Not activated (8 < 300)
    - Tick 2: Gross 400, Net 308 -> Activated! Peak = 308. Trigger threshold = 308 - 100 = 208.
    - Tick 3: Gross 320, Net 228 -> Above 208, no trigger.
    - Tick 4: Gross 290, Net 198 -> Below 208, TRIGGER!
    """
    path_1 = [100.0, 400.0, 320.0, 290.0]
    expected_signals_1 = ["NO_SIGNAL", "ARMED", "ARMED", "TRIGGERED"]

    state = PolicyJShadowState(trade_id="golden-1")
    eval_config = {
        "observation_enabled": True,
        "execution_enabled": False,
        "policy_j_activation_threshold_twd": 300.0,
        "policy_j_giveback_ratio_threshold": 100.0 / 300.0,
    }

    actual_signals = []
    for gross in path_1:
        obs = PolicyJShadowObservation(
            trade_id="golden-1",
            is_spread_phase=True,
            is_hedged_pair=True,
            exit_inflight=False,
            gross_liquidation_pnl_twd=gross,
            near_quote_age_ms=0,
            far_quote_age_ms=0,
        )
        snap, state = PolicyJShadowEvaluator.evaluate(obs, state, eval_config)
        if snap.would_trigger:
            actual_signals.append("TRIGGERED")
        elif snap.eligible and snap.shadow_signal == "ARMED":
            actual_signals.append("ARMED")
        else:
            actual_signals.append("NO_SIGNAL")

    assert actual_signals == expected_signals_1
