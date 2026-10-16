"""RED: the RECONCILED_EXIT_ONLY / operator attestation flow is
removed as an execution mode.  No EXIT_ONLY capability can authorize
or submit; legacy persisted EXIT_ONLY context stays fail-closed
(default-deny, never silently LIVE); normal LIVE_READY and PAPER order
authorization and the broker-truth live-UPL provenance are preserved.
"""
import json
from types import SimpleNamespace

import pytest

from core.mode_transition import (
    ExecutionContext, ExecutionMode, LiveOrderBlocked,
    ModeTransitionState)


def _live_ready_ctx():
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True)


def _exit_only_ctx():
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.RECONCILED_EXIT_ONLY.value,
        live_order_allowed=False,
        exit_only_capability={
            "reconciliation_id": "rid-1",
            "legs": [
                {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
                {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
            ],
        })


def _paper_ctx():
    return ExecutionContext(
        requested_mode="paper",
        effective_mode="paper_ready",
        live_order_allowed=True)


def _exit_order(rid="rid-1"):
    return SimpleNamespace(reconciliation_id=rid, strategy="MTS_EXIT")


def test_exit_only_capability_cannot_authorize_order():
    """A legacy RECONCILED_EXIT_ONLY capability is default-deny: the
    exit-only branch no longer authorizes any broker operation."""
    ctx = _exit_only_ctx()
    with pytest.raises(LiveOrderBlocked) as exc:
        ctx.assert_order_allowed(_exit_order(), method="place_order")
    assert "NOT_ORDER_AUTHORIZED" in str(exc.value)


def test_legacy_exit_only_context_never_silently_live():
    """A legacy persisted EXIT_ONLY context is NOT live-ready and never
    authorizes — fail-closed, never silently LIVE."""
    ctx = _exit_only_ctx()
    assert ctx.is_live_ready() is False
    assert ctx.live_order_allowed is False


def test_attestation_command_never_consumed(monkeypatch, tmp_path):
    """The operator attestation command is never consumed: no EXIT_ONLY
    capability can be authorized through it."""
    from strategies.futures.monitor import FuturesMonitor
    monkeypatch.setenv("LRC_RELEASE_SHA", "")
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.api = SimpleNamespace()
    m._execution_context = _exit_only_ctx()
    _p = tmp_path / "commands" / "reconciled_exit_attestation.json"
    _p.parent.mkdir(parents=True, exist_ok=True)
    _p.write_text(json.dumps({
        "command_id": "ATTEST-x", "action": "ATTEST_EXIT_ONLY",
        "created_at": 1, "operator": "o", "trade_id": "t",
        "evidence": "e", "expected_legs": []}), encoding="utf-8")
    assert m._process_reconciled_exit_attestation_command() is False
    assert _p.exists()  # never consumed, never authorized


def test_live_ready_order_authorization_preserved():
    """Normal LIVE_READY order authorization is preserved."""
    ctx = _live_ready_ctx()
    ctx.assert_order_allowed(_exit_order(), method="place_order")  # no raise


def test_paper_order_authorization_preserved():
    """Normal PAPER order authorization is preserved (no-op)."""
    ctx = _paper_ctx()
    ctx.assert_order_allowed(_exit_order(), method="place_order")  # no raise


def test_broker_truth_live_upl_provenance_preserved(tmp_path):
    """The broker-truth live-UPL provenance helper is preserved."""
    from core.performance_provenance import broker_snapshot_live_upl
    _diag = tmp_path / "diagnostics"
    _diag.mkdir(parents=True, exist_ok=True)
    _snap = _diag / "broker_snapshot_canonical.json"
    _snap.write_text(json.dumps({
        "source": "live_broker", "mode": "live",
        "account_identity_hash": "a" * 64,
        "canonical_input_hash": "c" * 64,
        "session_id": "sess-1",
        "captured_at": 1786637881606,
        "positions": [
            {"code": "TMFH6", "direction": "sell", "qty": 1,
             "avg_cost": 46077.0, "pnl": -3240.0},
            {"code": "TMFI6", "direction": "buy", "qty": 1,
             "avg_cost": 45231.0, "pnl": 3090.0},
        ],
    }), encoding="utf-8")
    _upl, _reason = broker_snapshot_live_upl(
        _snap, session_id="sess-1")
    assert _reason is None
    assert _upl == {"TMFH6": -3240.0, "TMFI6": 3090.0}
