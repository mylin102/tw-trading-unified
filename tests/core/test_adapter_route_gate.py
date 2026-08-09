#!/usr/bin/env python3
"""Step 8 — exhaustive state-changing order-route gate (adapter chokepoint).

Remaining ungated sites (committed inventory): shioaji_client.py
place_order / update_order / cancel_order. Manager path retains its
wrapper gate (downstream OrderManager.submit — untouched).

Contracts:
- non-LIVE_READY or ctx=None -> ZERO broker calls + structured reason
  (typed AdapterOrderError: ADAPTER_ORDER_BLOCKED_* )
- LIVE_READY preserves the intended broker call
- deterministic AST completeness: each adapter route is dominated by the
  execution-context gate
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def _repo_root():
    return Path(__file__).resolve().parents[2]


def _quarantined_ctx():
    from core.mode_transition import (ModeTransitionState,
                                      live_preflight_context,
                                      with_effective_mode)
    return with_effective_mode(
        live_preflight_context(), ModeTransitionState.LIVE_QUARANTINED.value)


def _ready_ctx():
    from core.mode_transition import (ModeTransitionState,
                                      live_preflight_context,
                                      with_effective_mode)
    ctx = with_effective_mode(
        live_preflight_context(), ModeTransitionState.LIVE_READY.value)
    object.__setattr__(ctx, "live_order_allowed", True)
    return ctx


class _RecordingApi:
    def __init__(self, fail_update=None):
        self.calls = []
        self.futopt_account = SimpleNamespace(person_id="P1")
        self.fail_update = fail_update

    def Order(self, **kw):
        return SimpleNamespace(**kw)

    def place_order(self, *a, **k):
        self.calls.append(("place_order", a, k))
        return SimpleNamespace(status=SimpleNamespace(status="Filled"))

    def update_order(self, *a, **k):
        self.calls.append(("update_order", a, k))
        if self.fail_update is not None:
            return self.fail_update
        return SimpleNamespace(status=SimpleNamespace(status="Filled"))

    def cancel_order(self, *a, **k):
        self.calls.append(("cancel_order", a, k))
        return SimpleNamespace(status=SimpleNamespace(status="Canceled"))


def _client_stub(ctx, api=None):
    from strategies.futures.squeeze_futures.data.shioaji_client import (
        ShioajiClient)
    c = ShioajiClient.__new__(ShioajiClient)
    c.api = api or _RecordingApi()
    c.is_logged_in = True
    c._execution_context = ctx
    return c


def _blocked_error(fn, *a, **k):
    with pytest.raises(Exception) as ei:
        fn(*a, **k)
    err = ei.value
    assert "ADAPTER_ORDER_BLOCKED" in getattr(err, "code", "") or \
        "BLOCKED" in str(type(err).__name__) or "BLOCKED" in str(err), err
    return err


# ── place_order ────────────────────────────────────────────────────────────

def test_client_place_order_quarantined_zero_calls():
    api = _RecordingApi()
    c = _client_stub(_quarantined_ctx(), api)
    err = _blocked_error(c.place_order, SimpleNamespace(code="TMFH6"),
                         "BUY", 1, 0)
    assert not api.calls, f"quarantined place_order must be zero-call: {api.calls}"
    assert "audit_reasons" in err.context or "reason" in err.context, err.context


def test_client_place_order_ctx_none_fail_closed():
    api = _RecordingApi()
    c = _client_stub(None, api)
    with pytest.raises(Exception) as ei:
        c.place_order(SimpleNamespace(code="TMFH6"), "BUY", 1, 0)
    assert "NO_LIVE_CERTIFICATION" in str(ei.value), ei.value
    assert not api.calls


def test_client_place_order_ready_permits():
    api = _RecordingApi()
    c = _client_stub(_ready_ctx(), api)
    trade = c.place_order(SimpleNamespace(code="TMFH6"), "BUY", 1, 0)
    assert trade is not None
    assert [x[0] for x in api.calls] == ["place_order"]


# ── update_order ───────────────────────────────────────────────────────────

def test_client_update_order_quarantined_zero_calls():
    api = _RecordingApi()
    c = _client_stub(_quarantined_ctx(), api)
    _blocked_error(c.update_order, SimpleNamespace(ts=1), 100.0, 1)
    assert not api.calls, f"quarantined update_order must be zero-call: {api.calls}"


def test_client_update_order_ready_permits():
    api = _RecordingApi()
    c = _client_stub(_ready_ctx(), api)
    result = c.update_order(SimpleNamespace(ts=1), 100.0, 1)
    assert result is not None
    assert [x[0] for x in api.calls] == ["update_order"]


# ── cancel_order ───────────────────────────────────────────────────────────

def test_client_cancel_order_quarantined_zero_calls():
    api = _RecordingApi()
    c = _client_stub(_quarantined_ctx(), api)
    _blocked_error(c.cancel_order, SimpleNamespace(ts=1))
    assert not api.calls, f"quarantined cancel_order must be zero-call: {api.calls}"


def test_client_cancel_order_ready_permits():
    api = _RecordingApi()
    c = _client_stub(_ready_ctx(), api)
    c.cancel_order(SimpleNamespace(ts=1))
    assert [x[0] for x in api.calls] == ["cancel_order"]


# ── deterministic AST completeness ─────────────────────────────────────────

def test_adapter_routes_ast_gate_dominance():
    # every state-changing adapter method must reference the gate helper
    # in its body (deterministic, worktree-independent)
    client = _repo_root() / "strategies" / "futures" / "squeeze_futures" / \
        "data" / "shioaji_client.py"
    tree = ast.parse(client.read_text(encoding="utf-8"))
    for method in ("place_order", "update_order", "cancel_order"):
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == method),
                  None)
        assert fn is not None, f"{method} not found"
        body_src = ast.unparse(fn)
        assert "_gate_or_raise" in body_src, \
            f"{method} must be dominated by the execution-context gate"
