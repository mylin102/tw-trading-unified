"""2026-10-30 Hermes Agent: combined_upl_exit_mode must survive the REAL
production callsite.

Incident: f4888739 added `combined_upl_exit_mode` to LifecycleContext and
tmf_spread put it into `strategy_state`, but MtsLifecycleAdapter.evaluate()
rebuilds LifecycleContext from that dict and never read the key, so PAPER
ran `giveback` for every exit (9/9 POLICY_J_LIVE_TRIGGERED events on Mini
after the deploy were POLICY_J_SPREAD_GIVEBACK, zero POLICY_J_FIXED_TARGET).
The original unit tests built LifecycleContext directly and so never
exercised this path. These tests go through the adapter entry point that
tmf_spread actually calls.
"""
import re
from pathlib import Path

from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    LifecycleAction,
    LifecycleEvaluationInput,
    MtsLifecycleAdapter,
    PolicyJTriggerPath,
    PositionLifecycle,
    PositionPhase,
    ReleaseGroupStatus,
)

ROOT = Path(__file__).resolve().parents[2]


def _evaluate(**overrides):
    lifecycle = PositionLifecycle(phase=PositionPhase.SPREAD)
    lifecycle.release_group.status = ReleaseGroupStatus.ARMED
    # total_net = (15 + 15) * 10 - 92 = 208 >= activation 200; peak never set.
    state = {
        "near_pnl_pts": 15.0, "far_pnl_pts": 15.0, "floating_pnl_pts": 30.0,
        "entry_age_secs": 120.0, "release_stop_threshold": 88.0, "trail_dist": 66.0,
        "enable_combined_upl_trail": True,
        "combined_upl_activation_net_pnl_twd": 200.0,
        "combined_upl_giveback_twd": 50.0,
        "peak_net_exit_pnl_twd": 0.0,
    }
    state.update(overrides)
    return MtsLifecycleAdapter().evaluate(LifecycleEvaluationInput(
        strategy_state=state, market_event={"event_time": "2026-10-30T10:00:00"},
        lifecycle=lifecycle, execution_mode="PAPER",
    ))


def test_adapter_honours_fixed_target_from_strategy_state():
    result = _evaluate(combined_upl_exit_mode="fixed_target")
    assert result.decision is not None
    assert result.decision.action == LifecycleAction.COMBINED_EXIT
    assert result.decision.winner == PolicyJTriggerPath.FIXED_TARGET.value


def test_adapter_default_mode_stays_giveback():
    # Same state without the key: giveback needs peak >= activation plus a
    # retrace, neither of which holds here -> no Policy J exit.
    result = _evaluate()
    assert result.decision is None or result.decision.winner != PolicyJTriggerPath.FIXED_TARGET.value


def test_every_combined_upl_key_tmf_spread_sends_is_read_by_adapter():
    """Wiring invariant: any combined_upl_* key that tmf_spread puts into the
    lifecycle strategy_state must be consumed by the adapter's state->context
    builder. A new knob added on one side only fails here."""
    sent = set(re.findall(r'"(combined_upl_[a-z_]+)":',
                          (ROOT / "strategies/plugins/futures/active/tmf_spread.py").read_text()))
    adapter = (ROOT / "strategies/plugins/futures/active/mts_lifecycle_adapter.py").read_text()
    read = set(re.findall(r'state\.get\(\s*"(combined_upl_[a-z_]+)"', adapter))
    assert sent, "no combined_upl_* keys found in tmf_spread strategy_state (regex drift?)"
    assert sent - read == set(), f"keys sent by tmf_spread but never read by adapter: {sorted(sent - read)}"
