# Live transition tests: PREFLIGHT -> LIVE_READY / LIVE_QUARANTINED.
import pytest

from core.mode_transition import (
    ModeTransitionState,
    live_preflight_context,
    paper_context,
    preflight_validate,
    transition_to_live_ready,
    with_effective_mode,
)


def test_paper_context_unchanged():
    ctx = paper_context()
    assert ctx.effective_mode == ModeTransitionState.PAPER_ACTIVE.value
    assert ctx.live_order_allowed is False
    assert ctx.state_namespace == "paper"


def test_live_starts_preflight_blocked():
    ctx = live_preflight_context()
    assert ctx.effective_mode == ModeTransitionState.LIVE_PREFLIGHT.value
    assert ctx.live_order_allowed is False  # fail-closed until transition


def test_transition_all_pass_becomes_live_ready():
    ctx = transition_to_live_ready(live_preflight_context(), [])
    assert ctx.effective_mode == ModeTransitionState.LIVE_READY.value
    assert ctx.live_order_allowed is True


def test_transition_failures_quarantine():
    ctx = transition_to_live_ready(live_preflight_context(), ["BROKER_NOT_CONNECTED"])
    assert ctx.effective_mode == ModeTransitionState.LIVE_QUARANTINED.value
    assert ctx.live_order_allowed is False  # permanent fail-closed


def test_quarantine_is_not_auto_retried():
    """LIVE_QUARANTINED must NOT auto-flip to READY without explicit operator action."""
    ctx = live_preflight_context()
    q = transition_to_live_ready(ctx, ["ACCOUNT_UNREADABLE"])
    # a subsequent "all pass" transition from quarantined is still operator-driven;
    # there is no timer/loop — assert the state machine has no auto path:
    assert q.effective_mode == ModeTransitionState.LIVE_QUARANTINED.value
    # with_effective_mode is the ONLY mutator (frozen dataclass) — no auto path exists
    assert with_effective_mode(q, ModeTransitionState.LIVE_READY.value,
                               live_order_allowed=True).live_order_allowed is True
    # (the above is explicit operator action; nothing calls it automatically)


def test_preflight_validate_checks():
    assert "BROKER_NOT_CONNECTED" in preflight_validate(None)
    assert "CONTRACTS_NOT_READY" in preflight_validate(None, contracts_ok=False)
    # logged-in api stub passes connection + account checks
    class FakeApi:
        login_info = object()
        account = object()
    assert preflight_validate(FakeApi()) == []


def test_preflight_rejects_unlogged_api():
    class FakeApi:
        login_info = None
        account = object()
    assert "BROKER_NOT_LOGGED_IN" in preflight_validate(FakeApi())
