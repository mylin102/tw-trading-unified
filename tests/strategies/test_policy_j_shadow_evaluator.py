# 2026-07-26 Gemini CLI: Unit tests for PolicyJShadowEvaluator
import pytest
from strategies.futures.mts.policy_j_shadow_evaluator import (
    PolicyJShadowEvaluator,
    PolicyJShadowObservation,
)
from strategies.futures.mts.policy_j_shadow_state import PolicyJShadowState
from strategies.futures.mts.policy_j_telemetry_schema import (
    EligibilityReason,
    PolicyJShadowSignal,
)


@pytest.fixture
def default_config():
    return {
        "shadow_enabled": True,
        "activation_net_pnl_twd": 300.0,
        "giveback_twd": 100.0,
    }


def test_evaluator_disabled_shadow(default_config):
    config = dict(default_config, shadow_enabled=False)
    obs = PolicyJShadowObservation(
        trade_id="T001",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=500.0,
        near_quote_age_ms=10,
        far_quote_age_ms=10,
    )
    state = PolicyJShadowState()
    snapshot, new_state = PolicyJShadowEvaluator.evaluate(obs, state, config)

    assert snapshot.eligible is False
    assert snapshot.eligibility_reason == EligibilityReason.POLICY_DISABLED.value
    assert snapshot.execution_blocked is True
    assert snapshot.would_trigger is False


def test_evaluator_not_spread_phase(default_config):
    obs = PolicyJShadowObservation(
        trade_id="T001",
        is_spread_phase=False,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=500.0,
        near_quote_age_ms=10,
        far_quote_age_ms=10,
    )
    state = PolicyJShadowState()
    snapshot, new_state = PolicyJShadowEvaluator.evaluate(obs, state, default_config)

    assert snapshot.eligible is False
    assert snapshot.eligibility_reason == EligibilityReason.NOT_SPREAD_PHASE.value
    assert snapshot.would_trigger is False


def test_evaluator_single_leg_only(default_config):
    obs = PolicyJShadowObservation(
        trade_id="T001",
        is_spread_phase=True,
        is_hedged_pair=False,
        exit_inflight=False,
        gross_liquidation_pnl_twd=500.0,
        near_quote_age_ms=10,
        far_quote_age_ms=10,
    )
    state = PolicyJShadowState()
    snapshot, new_state = PolicyJShadowEvaluator.evaluate(obs, state, default_config)

    assert snapshot.eligible is False
    assert snapshot.eligibility_reason == EligibilityReason.SINGLE_LEG_ONLY.value


def test_evaluator_stale_quote_does_not_update_peak(default_config):
    state = PolicyJShadowState(trade_id="T001", peak_net_exit_pnl_twd=200.0, sequence_no=1)
    
    # Near quote stale (1200ms > max 1000ms)
    obs = PolicyJShadowObservation(
        trade_id="T001",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=800.0,  # Would give 800 net, higher than peak 200
        near_quote_age_ms=1200,
        far_quote_age_ms=10,
    )
    snapshot, new_state = PolicyJShadowEvaluator.evaluate(obs, state, default_config)

    assert snapshot.eligible is False
    assert snapshot.eligibility_reason == EligibilityReason.NEAR_QUOTE_STALE.value
    assert snapshot.would_trigger is False
    # Crucial invariant: Peak MUST NOT be updated on stale quotes!
    assert new_state.peak_net_exit_pnl_twd == 200.0


def test_evaluator_activation_and_giveback_trigger_sequence(default_config):
    state = PolicyJShadowState(trade_id="T001")

    # Tick 1: Net PnL = 200 (Below activation 300)
    obs1 = PolicyJShadowObservation(
        trade_id="T001",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=200.0,
        near_quote_age_ms=10,
        far_quote_age_ms=10,
    )
    snap1, state1 = PolicyJShadowEvaluator.evaluate(obs1, state, default_config)
    assert snap1.eligible is True
    assert snap1.peak_net_exit_pnl_twd == 200.0
    assert snap1.would_trigger is False
    assert snap1.shadow_signal == PolicyJShadowSignal.MONITORING.value
    assert state1.armed is False

    # Tick 2: Net PnL = 500 (Crosses activation 300 -> Armed!)
    obs2 = PolicyJShadowObservation(
        trade_id="T001",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=500.0,
        near_quote_age_ms=10,
        far_quote_age_ms=10,
    )
    snap2, state2 = PolicyJShadowEvaluator.evaluate(obs2, state1, default_config)
    assert snap2.eligible is True
    assert snap2.peak_net_exit_pnl_twd == 500.0
    assert snap2.would_trigger is False
    assert snap2.shadow_signal == PolicyJShadowSignal.ARMED.value
    assert state2.armed is True

    # Tick 3: Net PnL = 390 (Drawdown = 110 >= giveback 100 -> WOULD_EXIT_BOTH!)
    obs3 = PolicyJShadowObservation(
        trade_id="T001",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=390.0,
        near_quote_age_ms=10,
        far_quote_age_ms=10,
        event_time="10:00:03",
    )
    snap3, state3 = PolicyJShadowEvaluator.evaluate(obs3, state2, default_config)
    assert snap3.eligible is True
    assert snap3.peak_net_exit_pnl_twd == 500.0
    assert snap3.would_trigger is True
    assert snap3.shadow_signal == PolicyJShadowSignal.WOULD_EXIT_BOTH.value
    assert snap3.execution_blocked is True
    assert snap3.first_trigger_event is True  # Edge transition!

    # Tick 4: Net PnL = 380 (Slightly lower, would_trigger still True, but NOT first_trigger_event!)
    obs4 = PolicyJShadowObservation(
        trade_id="T001",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=380.0,
        near_quote_age_ms=10,
        far_quote_age_ms=10,
        event_time="10:00:04",
    )
    snap4, state4 = PolicyJShadowEvaluator.evaluate(obs4, state3, default_config)
    assert snap4.would_trigger is True
    assert snap4.first_trigger_event is False  # Already emitted!

    # Tick 5: Duplicate Event Key (T001_10:00:04) -> Idempotency check!
    snap5, state5 = PolicyJShadowEvaluator.evaluate(obs4, state4, default_config)
    assert snap5.sequence_no == snap4.sequence_no  # Sequence number is NOT bumped!
    assert state5 == state4  # State is unmodified!
