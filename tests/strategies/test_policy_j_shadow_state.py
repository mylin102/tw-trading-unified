# 2026-07-26 Gemini CLI: Unit tests for PolicyJShadowState
import pytest
from strategies.futures.mts.policy_j_shadow_state import PolicyJShadowState


def test_shadow_state_immutability_and_serialization():
    state = PolicyJShadowState(
        trade_id="TRADE_001",
        peak_net_exit_pnl_twd=450.0,
        sequence_no=12,
        armed=True,
        would_trigger_emitted=False,
    )

    with pytest.raises(Exception):
        state.sequence_no = 13

    data_dict = state.to_dict()
    restored = PolicyJShadowState.from_dict(data_dict)
    assert restored == state


def test_shadow_state_trade_lifecycle_reset():
    state = PolicyJShadowState(
        trade_id="TRADE_001",
        peak_net_exit_pnl_twd=450.0,
        sequence_no=12,
        armed=True,
    )

    # Same trade_id retains state
    same_state = state.with_trade("TRADE_001")
    assert same_state == state

    # New trade_id resets sequence_no to 0 and peak to None
    new_state = state.with_trade("TRADE_002")
    assert new_state.trade_id == "TRADE_002"
    assert new_state.sequence_no == 0
    assert new_state.peak_net_exit_pnl_twd is None
    assert new_state.armed is False
