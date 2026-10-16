# 2026-07-26 Gemini CLI: Pure Policy J Unit Test Suite & Disabled Differential Parity Tests
import pytest

from strategies.plugins.futures.active.mts_lifecycle_adapter import PositionPhase
from strategies.futures.mts.combined_upl_trail_policy import (
    CombinedUplTrailAction,
    CombinedUplTrailConfig,
    CombinedUplTrailContext,
    CombinedUplTrailPolicy,
    CombinedUplTrailState,
    estimate_net_exit_pnl_twd,
)


def test_estimate_net_exit_pnl_twd_friction_contract():
    """Verify net exit PnL friction calculator contract and no double-counting rule."""
    # 1. Mid-price liquidation PnL with explicit spread cost
    net_pnl = estimate_net_exit_pnl_twd(
        gross_liquidation_pnl_twd=500.0,
        commission_twd=40.0,
        exchange_fee_twd=24.0,
        tax_twd=36.0,
        bid_ask_cost_twd=50.0,
        slippage_buffer_twd=20.0,
    )
    assert net_pnl == 330.0  # 500 - (40+24+36+50+20) = 330.0

    # 2. Executable quote liquidation PnL with bid_ask_cost_twd = 0 (no double count)
    net_executable = estimate_net_exit_pnl_twd(
        gross_liquidation_pnl_twd=450.0,
        commission_twd=40.0,
        exchange_fee_twd=24.0,
        tax_twd=36.0,
        bid_ask_cost_twd=0.0,
        slippage_buffer_twd=0.0,
    )
    assert net_executable == 350.0  # 450 - (40+24+36) = 350.0


def test_policy_j_disabled_by_default_safety_gate():
    """1. Verify enabled=False never triggers action and preserves state."""
    config = CombinedUplTrailConfig(enabled=False)
    state = CombinedUplTrailState()
    ctx = CombinedUplTrailContext(
        estimated_gross_liquidation_pnl_twd=1000.0,
        estimated_exit_friction_twd=100.0,
        phase=PositionPhase.SPREAD,
        near_open_qty=1,
        far_open_qty=1,
        has_exit_inflight=False,
        near_quote_age_ms=0, far_quote_age_ms=0,
    )
    action, new_state = CombinedUplTrailPolicy.evaluate(ctx, state, config)
    assert action == CombinedUplTrailAction.NO_ACTION
    assert new_state == state
    assert not new_state.activated


def test_policy_j_non_spread_phase_guard():
    """2. Verify non-full SPREAD positions cannot activate or trigger."""
    config = CombinedUplTrailConfig(enabled=True, activation_net_pnl_twd=300.0)
    state = CombinedUplTrailState()

    for phase in [PositionPhase.FLAT, PositionPhase.SINGLE_LEG]:
        ctx = CombinedUplTrailContext(
            estimated_gross_liquidation_pnl_twd=500.0,
            estimated_exit_friction_twd=50.0,
            phase=phase,
            near_open_qty=1 if phase == PositionPhase.SINGLE_LEG else 0,
            far_open_qty=0,
            has_exit_inflight=False,
            near_quote_age_ms=0, far_quote_age_ms=0,
        )
        action, new_state = CombinedUplTrailPolicy.evaluate(ctx, state, config)
        assert action == CombinedUplTrailAction.NO_ACTION
        assert not new_state.activated


def test_policy_j_market_quality_guards():
    """3. Verify stale quotes or exit inflight blocks policy evaluation."""
    config = CombinedUplTrailConfig(enabled=True, activation_net_pnl_twd=300.0)
    state = CombinedUplTrailState()

    # Case A: Exit inflight
    ctx_inflight = CombinedUplTrailContext(
        estimated_gross_liquidation_pnl_twd=600.0,
        estimated_exit_friction_twd=50.0,
        phase=PositionPhase.SPREAD,
        near_open_qty=1,
        far_open_qty=1,
        has_exit_inflight=True,
        near_quote_age_ms=0, far_quote_age_ms=0,
    )
    action, new_state = CombinedUplTrailPolicy.evaluate(ctx_inflight, state, config)
    assert action == CombinedUplTrailAction.NO_ACTION
    assert not new_state.activated

    # Case B: Stale quotes
    ctx_stale = CombinedUplTrailContext(
        estimated_gross_liquidation_pnl_twd=600.0,
        estimated_exit_friction_twd=50.0,
        phase=PositionPhase.SPREAD,
        near_open_qty=1,
        far_open_qty=1,
        has_exit_inflight=False,
        near_quote_age_ms=9999, far_quote_age_ms=0,
    )
    action, new_state = CombinedUplTrailPolicy.evaluate(ctx_stale, state, config)
    assert action == CombinedUplTrailAction.NO_ACTION
    assert not new_state.activated


def test_policy_j_activation_and_peak_tracking():
    """4-7. Verify activation threshold, peak tracking, and giveback trigger boundaries."""
    config = CombinedUplTrailConfig(enabled=True, activation_net_pnl_twd=300.0, giveback_twd=100.0)
    state = CombinedUplTrailState()

    # Step A: Net PnL 250 TWD (Below target -> unactivated)
    ctx_below = CombinedUplTrailContext(
        estimated_gross_liquidation_pnl_twd=300.0,
        estimated_exit_friction_twd=50.0,
        phase=PositionPhase.SPREAD,
        near_open_qty=1,
        far_open_qty=1,
        has_exit_inflight=False,
        near_quote_age_ms=0, far_quote_age_ms=0,
    )
    action, state = CombinedUplTrailPolicy.evaluate(ctx_below, state, config)
    assert action == CombinedUplTrailAction.NO_ACTION
    assert not state.activated

    # Step B: Net PnL 350 TWD (Above 300 TWD -> Activates, Peak = 350)
    ctx_activate = CombinedUplTrailContext(
        estimated_gross_liquidation_pnl_twd=400.0,
        estimated_exit_friction_twd=50.0,
        phase=PositionPhase.SPREAD,
        near_open_qty=1,
        far_open_qty=1,
        has_exit_inflight=False,
        near_quote_age_ms=0, far_quote_age_ms=0,
    )
    action, state = CombinedUplTrailPolicy.evaluate(ctx_activate, state, config)
    assert action == CombinedUplTrailAction.NO_ACTION
    assert state.activated
    assert state.peak_net_exit_pnl_twd == 350.0

    # Step C: Net PnL 450 TWD (New High -> Peak = 450)
    ctx_high = CombinedUplTrailContext(
        estimated_gross_liquidation_pnl_twd=500.0,
        estimated_exit_friction_twd=50.0,
        phase=PositionPhase.SPREAD,
        near_open_qty=1,
        far_open_qty=1,
        has_exit_inflight=False,
        near_quote_age_ms=0, far_quote_age_ms=0,
    )
    action, state = CombinedUplTrailPolicy.evaluate(ctx_high, state, config)
    assert action == CombinedUplTrailAction.NO_ACTION
    assert state.peak_net_exit_pnl_twd == 450.0

    # Step D: Net PnL 380 TWD (Giveback = 70 < 100 -> No Exit)
    ctx_minor_drop = CombinedUplTrailContext(
        estimated_gross_liquidation_pnl_twd=430.0,
        estimated_exit_friction_twd=50.0,
        phase=PositionPhase.SPREAD,
        near_open_qty=1,
        far_open_qty=1,
        has_exit_inflight=False,
        near_quote_age_ms=0, far_quote_age_ms=0,
    )
    action, state = CombinedUplTrailPolicy.evaluate(ctx_minor_drop, state, config)
    assert action == CombinedUplTrailAction.NO_ACTION
    assert state.peak_net_exit_pnl_twd == 450.0
    assert not state.triggered

    # Step E: Net PnL 350 TWD (Giveback = 100 == 100 -> Boundary Exit Triggered!)
    ctx_trigger = CombinedUplTrailContext(
        estimated_gross_liquidation_pnl_twd=400.0,
        estimated_exit_friction_twd=50.0,
        phase=PositionPhase.SPREAD,
        near_open_qty=1,
        far_open_qty=1,
        has_exit_inflight=False,
        near_quote_age_ms=0, far_quote_age_ms=0,
    )
    action, state = CombinedUplTrailPolicy.evaluate(ctx_trigger, state, config)
    assert action == CombinedUplTrailAction.TRIGGER_COMBINED_EXIT
    assert state.triggered


def test_policy_j_idempotency_and_serialization():
    """9-10. Verify state idempotency and dict serialization round-trip."""
    state = CombinedUplTrailState(activated=True, peak_net_exit_pnl_twd=500.0, triggered=True)
    config = CombinedUplTrailConfig(enabled=True)
    ctx = CombinedUplTrailContext(
        estimated_gross_liquidation_pnl_twd=300.0,
        estimated_exit_friction_twd=50.0,
        phase=PositionPhase.SPREAD,
        near_open_qty=1,
        far_open_qty=1,
        has_exit_inflight=False,
        near_quote_age_ms=0, far_quote_age_ms=0,
    )

    # Idempotency
    action, new_state = CombinedUplTrailPolicy.evaluate(ctx, state, config)
    assert action == CombinedUplTrailAction.TRIGGER_COMBINED_EXIT
    assert new_state == state

    # Dict serialization round-trip
    state_dict = state.to_dict()
    reconstructed = CombinedUplTrailState.from_dict(state_dict)
    assert reconstructed == state


def test_policy_j_disabled_decision_parity():
    """12. Verify 100% decision parity when enabled=False."""
    config_disabled = CombinedUplTrailConfig(enabled=False)
    state_disabled = CombinedUplTrailState()

    for pnl in [100.0, 500.0, 1000.0, -200.0]:
        ctx = CombinedUplTrailContext(
            estimated_gross_liquidation_pnl_twd=pnl,
            estimated_exit_friction_twd=50.0,
            phase=PositionPhase.SPREAD,
            near_open_qty=1,
            far_open_qty=1,
            has_exit_inflight=False, near_quote_age_ms=0, far_quote_age_ms=0,
        )
        action, new_state = CombinedUplTrailPolicy.evaluate(ctx, state_disabled, config_disabled)
        assert action == CombinedUplTrailAction.NO_ACTION
        assert new_state == state_disabled


class TestActivationInvariants:
    """Verify activation latch and trigger invariants (PR 2)."""

    def test_activation_latches_across_evaluations(self):
        """activated=True must remain True after PnL falls below threshold."""
        config = CombinedUplTrailConfig(enabled=True, activation_net_pnl_twd=300.0, giveback_twd=500.0)
        state = CombinedUplTrailState()

        # Step 1: Net 350 → activates, peak=350
        ctx = CombinedUplTrailContext(
            estimated_gross_liquidation_pnl_twd=400.0, estimated_exit_friction_twd=50.0,
            phase=PositionPhase.SPREAD, near_open_qty=1, far_open_qty=1,
            has_exit_inflight=False, near_quote_age_ms=0, far_quote_age_ms=0,
        )
        action, state = CombinedUplTrailPolicy.evaluate(ctx, state, config)
        assert action == CombinedUplTrailAction.NO_ACTION
        assert state.activated
        assert state.peak_net_exit_pnl_twd == 350.0

        # Step 2: Net 50 (well below 300) → activated must remain True
        ctx_low = CombinedUplTrailContext(
            estimated_gross_liquidation_pnl_twd=100.0, estimated_exit_friction_twd=50.0,
            phase=PositionPhase.SPREAD, near_open_qty=1, far_open_qty=1,
            has_exit_inflight=False, near_quote_age_ms=0, far_quote_age_ms=0,
        )
        action, state = CombinedUplTrailPolicy.evaluate(ctx_low, state, config)
        assert action == CombinedUplTrailAction.NO_ACTION
        assert state.activated, "activation must latch — cannot regress after PnL falls"

    def test_activation_does_not_regress_across_cycles(self):
        """Multiple evaluations after PnL drop must all preserve activated=True."""
        config = CombinedUplTrailConfig(enabled=True, activation_net_pnl_twd=300.0, giveback_twd=100.0)
        state = CombinedUplTrailState()

        # Activate
        ctx = CombinedUplTrailContext(
            estimated_gross_liquidation_pnl_twd=400.0, estimated_exit_friction_twd=50.0,
            phase=PositionPhase.SPREAD, near_open_qty=1, far_open_qty=1,
            has_exit_inflight=False, near_quote_age_ms=0, far_quote_age_ms=0,
        )
        _, state = CombinedUplTrailPolicy.evaluate(ctx, state, config)
        assert state.activated

        # 5 cycles at low PnL — each must preserve activated=True
        for i in range(5):
            ctx_low = CombinedUplTrailContext(
                estimated_gross_liquidation_pnl_twd=100.0, estimated_exit_friction_twd=50.0,
                phase=PositionPhase.SPREAD, near_open_qty=1, far_open_qty=1,
                has_exit_inflight=False, near_quote_age_ms=0, far_quote_age_ms=0,
            )
            _, state = CombinedUplTrailPolicy.evaluate(ctx_low, state, config)
            assert state.activated, f"activation regressed at cycle {i}"

    def test_trigger_implies_activated(self):
        """Invariant: would_trigger=True ⇒ activated=True."""
        config = CombinedUplTrailConfig(enabled=True, activation_net_pnl_twd=200.0, giveback_twd=50.0)
        state = CombinedUplTrailState()

        # Activate at peak 300
        ctx_activate = CombinedUplTrailContext(
            estimated_gross_liquidation_pnl_twd=350.0, estimated_exit_friction_twd=50.0,
            phase=PositionPhase.SPREAD, near_open_qty=1, far_open_qty=1,
            has_exit_inflight=False, near_quote_age_ms=0, far_quote_age_ms=0,
        )
        _, state = CombinedUplTrailPolicy.evaluate(ctx_activate, state, config)
        assert state.activated
        assert state.peak_net_exit_pnl_twd == 300.0

        # Trigger at net 200 (giveback = 100 > 50)
        ctx_trigger = CombinedUplTrailContext(
            estimated_gross_liquidation_pnl_twd=250.0, estimated_exit_friction_twd=50.0,
            phase=PositionPhase.SPREAD, near_open_qty=1, far_open_qty=1,
            has_exit_inflight=False, near_quote_age_ms=0, far_quote_age_ms=0,
        )
        action, state = CombinedUplTrailPolicy.evaluate(ctx_trigger, state, config)
        assert action == CombinedUplTrailAction.TRIGGER_COMBINED_EXIT
        assert state.activated, "Invariant violated: would_trigger=True but activated=False"
        assert state.triggered

    def test_peak_never_decreases_within_trade(self):
        """Peak must be monotonic non-decreasing for same trade."""
        config = CombinedUplTrailConfig(enabled=True, activation_net_pnl_twd=100.0, giveback_twd=200.0)
        state = CombinedUplTrailState()
        peaks_seen = []

        for gross in [150.0, 300.0, 450.0, 200.0, 500.0, 100.0]:
            ctx = CombinedUplTrailContext(
                estimated_gross_liquidation_pnl_twd=gross, estimated_exit_friction_twd=50.0,
                phase=PositionPhase.SPREAD, near_open_qty=1, far_open_qty=1,
                has_exit_inflight=False, near_quote_age_ms=0, far_quote_age_ms=0,
            )
            _, state = CombinedUplTrailPolicy.evaluate(ctx, state, config)
            if state.peak_net_exit_pnl_twd is not None:
                peaks_seen.append(state.peak_net_exit_pnl_twd)
                if len(peaks_seen) >= 2:
                    assert peaks_seen[-1] >= peaks_seen[-2], \
                        f"peak decreased: {peaks_seen[-2]} → {peaks_seen[-1]}"
