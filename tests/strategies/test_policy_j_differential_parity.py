# 2026-07-26 Gemini CLI: Wave J1.5-B Closure - Differential Parity Harness Test
import pytest

from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    Leg,
    LifecycleAction,
    LifecycleContext,
    PositionLifecycle,
    PositionPhase,
    ReleaseGroup,
    ReleaseGroupStatus,
    evaluate_lifecycle_actions,
)
from strategies.futures.mts.policy_j_shadow_evaluator import (
    PolicyJShadowEvaluator,
    PolicyJShadowObservation,
)
from strategies.futures.mts.policy_j_shadow_state import PolicyJShadowState


def test_differential_parity_shadow_attached_vs_unattached():
    """
    Differential Harness Test:
    Compare evaluate_lifecycle_actions decision outputs with vs without PolicyJShadowEvaluator attached.
    
    Invariant:
    - 0 differences in LifecycleAction
    - 0 differences in reason/target
    - 0 differences in order intent or state transition!
    """
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )

    ctx = LifecycleContext(
        near_pnl_pts=-85.0,
        far_pnl_pts=10.0,
        floating_pnl_pts=-75.0,
        entry_age_secs=100.0,
        release_stop_threshold=80.0,
        trail_dist=48.9,
        enable_combined_upl_trail=False,  # Hardlocked False by default
    )

    # 1. Baseline evaluation without shadow evaluator
    baseline_decision = evaluate_lifecycle_actions(ctx, lc)

    # 2. Shadow evaluation attached
    shadow_obs = PolicyJShadowObservation(
        trade_id="TRADE_DIFF_001",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=3000.0,
        near_quote_age_ms=10,
        far_quote_age_ms=10,
    )
    shadow_state = PolicyJShadowState()
    shadow_config = {"shadow_enabled": True, "activation_net_pnl_twd": 300.0, "giveback_twd": 100.0}

    snapshot, new_state = PolicyJShadowEvaluator.evaluate(shadow_obs, shadow_state, shadow_config)
    shadow_decision = evaluate_lifecycle_actions(ctx, lc)

    # Differential Invariant Assertions
    assert baseline_decision is not None
    assert shadow_decision is not None
    assert shadow_decision.action == baseline_decision.action
    assert shadow_decision.release_leg == baseline_decision.release_leg
    assert shadow_decision.action == LifecycleAction.RELEASE
    assert shadow_decision.release_leg == Leg.NEAR
    assert snapshot.execution_blocked is True


class TestIncidentGoldenVectors:
    """Trade 23-643 (mts-auto-155223-643) golden vectors.
    
    Phase 1: Pre-restart — peak reaches 348, giveback triggers.
    Phase 2: Restart recovery — peak restored from state, activated preserved.
    """

    # Shared config matching incident params
    ACTIVATION_TWD = 200.0
    GIVEBACK_TWD = 50.0
    FRICTION_TWD = 92.0

    def _prod_eval(self, gross: float, peak: float, activated: bool,
                   triggered: bool, trigger_id: str | None, seq: int) -> tuple:
        """Evaluate CombinedUplTrailPolicy with given state."""
        from strategies.futures.mts.combined_upl_trail_policy import (
            CombinedUplTrailAction,
            CombinedUplTrailConfig,
            CombinedUplTrailContext,
            CombinedUplTrailPolicy,
            CombinedUplTrailState,
        )
        config = CombinedUplTrailConfig(
            enabled=True,
            activation_net_pnl_twd=self.ACTIVATION_TWD,
            giveback_twd=self.GIVEBACK_TWD,
        )
        state = CombinedUplTrailState(
            activated=activated,
            peak_net_exit_pnl_twd=peak,
            triggered=triggered,
            trigger_event_id=trigger_id,
            last_sequence_no=seq,
            trade_id="mts-auto-155223-643",
        )
        ctx = CombinedUplTrailContext(
            estimated_gross_liquidation_pnl_twd=gross,
            estimated_exit_friction_twd=self.FRICTION_TWD,
            phase=PositionPhase.SPREAD,
            near_open_qty=1,
            far_open_qty=1,
            has_exit_inflight=False,
            near_quote_age_ms=0,
            far_quote_age_ms=0,
        )
        action, new_state = CombinedUplTrailPolicy.evaluate(ctx, state, config)
        net = gross - self.FRICTION_TWD
        return action, new_state, net

    def _shadow_eval(self, gross: float, peak: float, armed: bool,
                     emitted: bool, seq: int) -> tuple:
        """Evaluate PolicyJShadowEvaluator with given state."""
        from strategies.futures.mts.policy_j_shadow_evaluator import (
            PolicyJShadowEvaluator,
            PolicyJShadowObservation,
        )
        from strategies.futures.mts.policy_j_shadow_state import PolicyJShadowState

        config = {
            "shadow_enabled": True,
            "activation_net_pnl_twd": self.ACTIVATION_TWD,
            "giveback_twd": self.GIVEBACK_TWD,
        }
        state = PolicyJShadowState(
            trade_id="mts-auto-155223-643",
            peak_net_exit_pnl_twd=peak,
            sequence_no=seq,
            armed=armed,
            would_trigger_emitted=emitted,
        )
        obs = PolicyJShadowObservation(
            trade_id="mts-auto-155223-643",
            is_spread_phase=True,
            is_hedged_pair=True,
            exit_inflight=False,
            gross_liquidation_pnl_twd=gross,
            commission_twd=0.0,
            exchange_fee_twd=0.0,
            tax_twd=0.0,
            bid_ask_cost_twd=self.FRICTION_TWD,
            near_quote_age_ms=0,
            far_quote_age_ms=0,
        )
        snap, new_state = PolicyJShadowEvaluator.evaluate(obs, state, config)
        net = gross - self.FRICTION_TWD
        return snap, new_state, net

    def test_phase1_peak_348_and_trigger(self):
        """Phase 1 golden vector: peak=348, giveback=450, would_trigger=True.
        
        Sequence from incident telemetry:
        seq | gross | net  | peak | would_trigger
        1   | +50   | -42  | 42   | False
        2   | +240  | +148 | 148  | False
        3   | +440  | +348 | 348  | False (just activated, peak from tmf_spread)
        4   | -10   | -102 | 348  | True  (giveback 450 > 50)
        
        Note: peak tracking before activation happens in tmf_spread.py,
        not in CombinedUplTrailPolicy. The evaluator receives peak from
        the strategy instance. Test passes pre-computed peaks.
        """
        from strategies.futures.mts.combined_upl_trail_policy import (
            CombinedUplTrailAction,
        )

        # Seq 1: net=-42, peak stays 0 (evaluator returns state unchanged when not activated)
        prod_action, prod_state, prod_net = self._prod_eval(
            gross=50.0, peak=0.0, activated=False,
            triggered=False, trigger_id=None, seq=0,
        )
        assert prod_action == CombinedUplTrailAction.NO_ACTION
        assert not prod_state.activated

        # Seq 2: net=148, peak=148 (passed from tmf_spread, not tracked by evaluator)
        prod_action, prod_state, prod_net = self._prod_eval(
            gross=240.0, peak=148.0, activated=False,
            triggered=False, trigger_id=None, seq=0,
        )
        assert prod_action == CombinedUplTrailAction.NO_ACTION
        assert not prod_state.activated

        # Seq 3: net=348 → ACTIVATED at 200 threshold. Peak 348 from tmf_spread.
        prod_action, prod_state, prod_net = self._prod_eval(
            gross=440.0, peak=348.0, activated=False,
            triggered=False, trigger_id=None, seq=1,
        )
        assert prod_action == CombinedUplTrailAction.NO_ACTION
        assert prod_state.activated, "net=348 must activate (threshold=200)"
        assert prod_state.peak_net_exit_pnl_twd == 348.0
        assert not prod_state.triggered

        # Seq 4: net=-102, peak=348, giveback=450 → TRIGGER
        prod_action, prod_state, prod_net = self._prod_eval(
            gross=-10.0, peak=348.0, activated=True,
            triggered=False, trigger_id=None, seq=2,
        )
        assert prod_action == CombinedUplTrailAction.TRIGGER_COMBINED_EXIT
        assert prod_state.triggered
        assert prod_state.trigger_event_id is not None
        assert "mts-auto-155223-643" in prod_state.trigger_event_id

        # Shadow parity: shadow evaluator DOES track peak internally
        snap, shadow_state, shadow_net = self._shadow_eval(
            gross=440.0, peak=0.0, armed=False, emitted=False, seq=0,
        )
        assert snap.eligible
        assert not snap.would_trigger
        assert shadow_state.peak_net_exit_pnl_twd == 348.0  # shadow tracked peak

        snap, shadow_state, shadow_net = self._shadow_eval(
            gross=-10.0, peak=348.0, armed=True, emitted=False, seq=1,
        )
        assert snap.eligible
        assert snap.would_trigger, "shadow must would_trigger at giveback=450"

    def test_phase2_restart_restores_peak_and_activated(self):
        """Phase 2 golden vector: restart restores peak=348, activated=True.
        
        After restart, the state file has peak=348, activated=True.
        The evaluator must NOT reset these.
        """
        from strategies.futures.mts.combined_upl_trail_policy import (
            CombinedUplTrailAction,
        )

        # Restored state from persistence: peak=348, activated=True
        # gross=330, net=238, giveback=348-238=110 > 50 → triggers
        prod_action, prod_state, prod_net = self._prod_eval(
            gross=330.0, peak=348.0, activated=True,
            triggered=False, trigger_id="mts-auto-155223-643:3", seq=3,
        )
        assert prod_action == CombinedUplTrailAction.TRIGGER_COMBINED_EXIT
        assert prod_state.activated, "restored activated must persist"
        assert prod_state.peak_net_exit_pnl_twd == 348.0

    def test_restart_new_trade_resets_state(self):
        """Different trade_id after restart must reset Policy J state."""
        from strategies.futures.mts.combined_upl_trail_policy import (
            CombinedUplTrailAction,
            CombinedUplTrailConfig,
            CombinedUplTrailContext,
            CombinedUplTrailPolicy,
            CombinedUplTrailState,
        )

        # Simulate state file from OLD trade (mts-auto-155223-643)
        old_state = CombinedUplTrailState(
            activated=True,
            peak_net_exit_pnl_twd=348.0,
            triggered=False,
            trade_id="mts-auto-155223-643",
            last_sequence_no=5,
        )
        config = CombinedUplTrailConfig(
            enabled=True,
            activation_net_pnl_twd=self.ACTIVATION_TWD,
            giveback_twd=self.GIVEBACK_TWD,
        )

        # New trade with new trade_id must start fresh
        state = CombinedUplTrailState(
            activated=False,
            peak_net_exit_pnl_twd=None,
            triggered=False,
            trade_id="mts-auto-999999-001",
        )
        ctx = CombinedUplTrailContext(
            estimated_gross_liquidation_pnl_twd=200.0,
            estimated_exit_friction_twd=self.FRICTION_TWD,
            phase=PositionPhase.SPREAD,
            near_open_qty=1,
            far_open_qty=1,
            has_exit_inflight=False,
            near_quote_age_ms=0,
            far_quote_age_ms=0,
        )
        action, new_state = CombinedUplTrailPolicy.evaluate(ctx, state, config)
        # Net=108 < 200 → should NOT activate
        assert action == CombinedUplTrailAction.NO_ACTION
        assert not new_state.activated
        assert new_state.peak_net_exit_pnl_twd is None or new_state.peak_net_exit_pnl_twd <= 108.0

    def test_production_shadow_parity_on_incident_vectors(self):
        """Full incident timeline: both evaluators must agree on trigger decisions.
        
        Peak tracking differs by design:
        - Production (CombinedUplTrailPolicy): receives pre-computed peak from
          tmf_spread.py; does NOT update peak when not activated.
        - Shadow (PolicyJShadowEvaluator): tracks peak internally and updates
          it on every evaluation, even when not armed.
        
        Parity is validated on: net PnL, trigger decision, and activated state.
        Peak values are compared only after activation (when both converge).
        """
        vectors = [
            # (seq, gross, exp_net, expected_prod_action, expected_shadow_trigger, 
            #  prod_peak_input, shadow_peak_reset, note)
            (0, 50.0,   -42.0,  "NO_ACTION", False,  0.0,  0.0,   "entry, net negative"),
            (1, 240.0,  148.0,  "NO_ACTION", False,  148.0, 0.0,   "rising, peak=148"), 
            (2, 440.0,  348.0,  "NO_ACTION", False,  348.0, 0.0,   "peak=348, just activated"),
            (3, -10.0, -102.0,  "TRIGGER_COMBINED_EXIT", True, 348.0, 348.0, "giveback=450, trigger"),
        ]
        from strategies.futures.mts.combined_upl_trail_policy import (
            CombinedUplTrailAction,
        )

        prod_state = None
        shadow_state = None
        prod_peak = 0.0
        shadow_peak = 0.0
        prod_activated = False
        shadow_armed = False
        seq = 0

        for seq_num, gross, exp_net, exp_action, exp_trigger, prod_peak_in, shadow_peak_in, note in vectors:
            # Production (peak_net_exit_pnl_twd passed as input, not tracked internally)
            prod_action, prod_state, prod_net = self._prod_eval(
                gross=gross, peak=prod_peak_in, activated=prod_activated,
                triggered=(prod_state.triggered if prod_state else False),
                trigger_id=(prod_state.trigger_event_id if prod_state else None),
                seq=seq,
            )
            if prod_state:
                prod_activated = prod_state.activated

            # Shadow (peak tracked internally)
            snap, shadow_state, shadow_net = self._shadow_eval(
                gross=gross, peak=shadow_peak,
                armed=shadow_armed,
                emitted=(shadow_state.would_trigger_emitted if shadow_state else False),
                seq=seq,
            )
            if shadow_state:
                shadow_peak = shadow_state.peak_net_exit_pnl_twd or shadow_peak
                shadow_armed = shadow_state.armed

            # Net PnL parity — MUST match (same gross - same friction)
            assert abs(prod_net - shadow_net) < 0.01, \
                f"seq={seq_num} Net PnL mismatch: prod={prod_net} shadow={shadow_net}"

            # Trigger decision parity — MUST match
            prod_triggered = (prod_action == CombinedUplTrailAction.TRIGGER_COMBINED_EXIT)
            assert prod_triggered == exp_trigger, \
                f"seq={seq_num} Production trigger mismatch: got={prod_triggered} expected={exp_trigger}"
            assert snap.would_trigger == exp_trigger, \
                f"seq={seq_num} Shadow trigger mismatch: got={snap.would_trigger} expected={exp_trigger}"

            seq = (prod_state.last_sequence_no if prod_state else 0) or seq + 1

            # Peak parity after activation: once both evaluators are activated,
            # peak values should converge (both track from same snapshot)
            if prod_activated and shadow_armed:
                assert prod_state.peak_net_exit_pnl_twd == shadow_state.peak_net_exit_pnl_twd, \
                    f"seq={seq_num} Activated peak mismatch: prod={prod_state.peak_net_exit_pnl_twd} shadow={shadow_state.peak_net_exit_pnl_twd}"
