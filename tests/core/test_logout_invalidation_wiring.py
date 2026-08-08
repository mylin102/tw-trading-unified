#!/usr/bin/env python3
"""Step 7 — logout/session invalidation (research/core wiring only).

Contracts:
- real shioaji_session.logout invalidates the session registry generation
  BEFORE broker logout (centralized); unregister failure -> invalidate_all
  + re-raise (fail-closed)
- old route certificates must fail validation/transition after logout
- monitor context -> LIVE_QUARANTINED with SESSION_LOGOUT audit reason
- PAPER behavior untouched
"""

from types import SimpleNamespace

import pytest


def _ready_ctx():
    from core.mode_transition import (ModeTransitionState,
                                      live_preflight_context,
                                      with_effective_mode)
    ctx = with_effective_mode(
        live_preflight_context(), ModeTransitionState.LIVE_READY.value)
    object.__setattr__(ctx, "live_order_allowed", True)
    return ctx


def _monitor_stub(ctx):
    from strategies.futures.monitor import FuturesMonitor
    m = FuturesMonitor.__new__(FuturesMonitor)
    m._execution_context = ctx
    m.live_trading = True
    m.dry_run = False
    m.contract = SimpleNamespace(code="TMFH6")
    m.api = SimpleNamespace(calls=[])
    m.client = SimpleNamespace(calls=[])
    m.config_path = "/tmp/futures_test.yaml"
    return m


def test_logout_quarantines_monitor_context(tmp_path, monkeypatch):
    # monitor must not retain LIVE_READY across broker logout: quarantine
    # + SESSION_LOGOUT + persist to the canonical file
    from core.execution_context_state import read_execution_context
    m = _monitor_stub(_ready_ctx())
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    m._on_session_logout()
    assert not m._execution_context.is_live_ready()
    assert "SESSION_LOGOUT" in m._execution_context.audit_reasons, \
        m._execution_context.audit_reasons
    data = read_execution_context(runtime_dir=str(tmp_path))
    assert data["effective_mode"] == "live_quarantined"
    assert data["live_order_allowed"] is False
    assert "SESSION_LOGOUT" in data["audit_reasons"], data


def test_logout_quarantine_zero_order_calls():
    # after logout the per-route gates block: zero broker calls
    m = _monitor_stub(_ready_ctx())
    m._on_session_logout()
    result = m._place_safety_stop(44300, "LONG", 1, 50)
    assert not m.api.calls
    assert isinstance(result, dict) and result.get("blocked") is True


def test_old_cert_rejected_after_logout():
    # a certificate issued for a session generation is invalid once the
    # session is unregistered: transition_with_certificate must fail
    # closed (never LIVE_READY with a stale cert)
    import importlib.util
    from pathlib import Path
    from core.live_route_certificate import (
        session_registry, transition_with_certificate)
    from core.mode_transition import live_preflight_context
    helpers_path = Path(__file__).resolve().parent / \
        "test_live_route_certificate.py"
    spec = importlib.util.spec_from_file_location("_lrc_helpers", helpers_path)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    api = h._FakeApi()
    cert, failures, issuer = h._certify(api)
    assert cert is not None, failures
    runtime = h._ctx_runtime(api)
    result = transition_with_certificate(live_preflight_context(), cert,
                                         issuer, runtime=runtime)
    assert result.is_live_ready(), "pre-logout cert must reach LIVE_READY"
    # broker logout: registry unregister (centralized invalidation)
    session_registry.unregister(api)
    assert session_registry.current_generation() is None
    runtime_after = h._ctx_runtime(api)
    result_after = transition_with_certificate(
        live_preflight_context(), cert, issuer, runtime=runtime_after)
    assert not result_after.is_live_ready(), \
        "old certificate must FAIL validation/transition after logout"


def test_invalidation_failure_fail_closed(monkeypatch):
    # unregister failure -> global revoke (invalidate_all) + re-raise:
    # broker logout must never proceed with a valid generation
    import core.shioaji_session as shs
    from core.live_route_certificate import session_registry
    fake_api = SimpleNamespace(logout=lambda: None)

    def _boom_unregister(api):
        raise RuntimeError("unregister failed")

    monkeypatch.setattr(session_registry, "unregister", _boom_unregister)
    revoked = []
    real_invalidate_all = session_registry.invalidate_all
    monkeypatch.setattr(
        session_registry, "invalidate_all",
        lambda: (revoked.append(True), real_invalidate_all())[1])
    monkeypatch.setattr(shs, "_api", fake_api)
    with pytest.raises(RuntimeError):
        shs.logout()
    assert revoked == [True], "unregister failure must force global revocation"
    assert session_registry.current_generation() is None
    monkeypatch.undo()
    session_registry._entries.clear()
    session_registry._last_generation = None
    session_registry._last_generation_api = None


def test_paper_behavior_preserved(tmp_path, monkeypatch):
    # logout invalidation never touches PAPER contexts
    from core.mode_transition import paper_context
    m = _monitor_stub(paper_context(account_id="A1"))
    m.live_trading = False  # paper mode never engages the LIVE route
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    m._on_session_logout()
    assert not m._execution_context.is_live_ready()
    # paper mode: gates do NOT raise for paper orders (legal by design)
    m._place_safety_stop(44300, "LONG", 1, 50)
    assert not m.api.calls, "paper safety-stop must still be gate-transparent"
    # paper ctx is not LIVE and can never become LIVE_READY
    assert m._execution_context.to_dict().get("requested_mode") == "paper"
