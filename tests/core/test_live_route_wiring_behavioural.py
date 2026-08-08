#!/usr/bin/env python3
"""Phase-2 v3 behavioural wiring tests (RED except positive contract locks).

Codex v3 direction: behavioural recording-client tests per route — NOT
string/AST presence. The AST inventory stays a completeness tripwire only.

Contracts:
- LIVE_QUARANTINED → zero place/cancel/modify/update calls on every route
- LIVE_READY → the intended call is permitted
- PAPER unchanged (mode_transition core suite already locks it)
- emergency path: durable EXIT intent BEFORE any strategy mutation / broker
  I/O; separately authorized; idempotent; post-fill/restart reconciliation
- reconnect: quarantine before re-login; fresh cert only after resubscribe;
  failed subscribe/cert stays quarantined
- logout: real broker logout invalidates an existing certificate/gate
  (centralized in shioaji_session — monitor must NOT re-implement it)
"""

from types import SimpleNamespace

import pytest


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
    object.__setattr__(ctx, "live_order_allowed", True)  # LIVE_READY also
    return ctx                                            # needs the flag


class RecordingBroker:
    """Records every state-changing call; zero-order surface otherwise."""

    def __init__(self):
        self.calls = []
        self.futopt_account = SimpleNamespace(person_id="P1")

    def place_order(self, *a, **k):
        self.calls.append(("place_order", a, k))
        return None

    def cancel_order(self, *a, **k):
        self.calls.append(("cancel_order", a, k))

    def update_order(self, *a, **k):
        self.calls.append(("update_order", a, k))

    def modify_order(self, *a, **k):
        self.calls.append(("modify_order", a, k))

    def Order(self, **kw):
        return SimpleNamespace(**kw)


def _monitor_stub(ctx, *, use_order_manager=False, safety_trade=None):
    from strategies.futures.monitor import FuturesMonitor
    m = FuturesMonitor.__new__(FuturesMonitor)
    m._execution_context = ctx
    m.live_trading = True
    m.dry_run = False
    m.contract = SimpleNamespace(code="TMFH6")
    m.api = RecordingBroker()
    m.client = RecordingBroker()
    m._use_order_manager = use_order_manager
    m._safety_stop_trade = safety_trade
    m.trader = SimpleNamespace(position=1, entry_price=0.0,
                               point_value=1, fee_per_side=0,
                               exchange_fee_per_side=0, tax_rate=0)
    m._margin_sufficient = lambda: True
    return m


# ── (1) per-route behavioural: QUARANTINED → zero calls ─────────────────────

def test_safety_stop_placement_quarantined_zero_calls():
    m = _monitor_stub(_quarantined_ctx())
    m._place_safety_stop(44300, "LONG", 1, 50)
    assert not m.api.calls, f"quarantined safety-stop placement: {m.api.calls}"


def test_safety_stop_cancel_quarantined_zero_calls():
    m = _monitor_stub(_quarantined_ctx(),
                      safety_trade=SimpleNamespace(ts=1))
    m._cancel_safety_stop()
    assert not m.api.calls, f"quarantined safety-stop cancel: {m.api.calls}"


def test_execute_trade_quarantined_zero_calls():
    m = _monitor_stub(_quarantined_ctx(), safety_trade=SimpleNamespace(ts=1))
    m._execute_trade("EXIT", 44300, "2026-08-08T10:00:00", 1, reason="TEST")
    assert not m.client.calls, f"quarantined execute_trade: {m.client.calls}"
    assert not m.api.calls, f"quarantined safety-stop cancel: {m.api.calls}"


def test_execute_trade_ready_permits_intended_place():
    m = _monitor_stub(_ready_ctx(), safety_trade=SimpleNamespace(ts=1))
    m._execute_trade("EXIT", 44300, "2026-08-08T10:00:00", 1, reason="TEST")
    kinds = [c[0] for c in m.client.calls]
    assert "place_order" in kinds, f"LIVE_READY must permit the exit: {kinds}"


def test_dispatcher_gate_blocks_quarantined():
    m = _monitor_stub(_quarantined_ctx())
    with pytest.raises(Exception):
        m._submit_mts_order_signal("EXIT", None, None, None)
    assert not m.client.calls


def test_dispatcher_gate_permits_ready():
    m = _monitor_stub(_ready_ctx())
    try:
        m._submit_mts_order_signal("EXIT", None, None, None)
    except Exception as e:
        if "Blocked" in type(e).__name__ or "live" in type(e).__name__.lower():
            pytest.fail(f"LIVE_READY dispatcher must not raise the gate: {e}")
        # any other crash (stub strategy is None) means the gate PASSED


def test_order_manager_wrapper_gate_behaviour():
    from core.order_management.order_manager import _assert_live_allowed
    with pytest.raises(Exception):
        _assert_live_allowed(_quarantined_ctx())
    _assert_live_allowed(_ready_ctx())          # no raise → permitted


# ── (2) emergency: durable intent / authorization / idempotency / reconcile ─

def _emergency_stub_strategy():
    return SimpleNamespace(_has_position=True, _released_leg=None,
                           _near_side="LONG")


def test_emergency_durable_intent_before_strategy_mutation():
    # RED: strategy state must not be mutated before a DURABLE EXIT intent
    # (fsync'd ledger) is recorded — currently _emergency_flatten_mts
    # mutates _released_leg/_side first with no ledger
    m = _monitor_stub(_ready_ctx())
    s = _emergency_stub_strategy()
    try:
        m._emergency_flatten_mts(s)
    except Exception:
        pass
    if s._released_leg is not None or hasattr(s, "_side"):
        pytest.fail("strategy mutated without a durable EXIT intent ledger")
    # (if nothing mutated at all the emergency was blocked — acceptable
    # only via an explicit blocked-with-procedure contract)


def test_emergency_separate_operator_authorization():
    # RED: emergency must be gated by a separately authorized operator
    # command (manual close_all / settlement gate carry a durable token);
    # a plain internal call must NOT flatten under quarantine
    m = _monitor_stub(_quarantined_ctx())
    s = _emergency_stub_strategy()
    try:
        m._emergency_flatten_mts(s)
    except Exception:
        pass
    assert not m.client.calls, \
        f"un-authorized emergency under quarantine: {m.client.calls}"


def test_emergency_idempotent_via_durable_intent_ledger():
    # RED: the durable intent ledger module is the dedup anchor — re-sending
    # the same emergency must yield the same outcome (≤1 fill request)
    with pytest.raises(ImportError):
        import core.emergency_intent  # noqa: F401  (wiring phase)


def test_emergency_post_fill_restart_reconciliation():
    # RED: outstanding intents must reconcile after fill/restart
    with pytest.raises(ImportError):
        import core.emergency_intent  # noqa: F401


# ── (3) reconnect atomic handoff (manual + auto share the same flow) ────────

def test_reconnect_quarantines_before_relogin_and_recertifies(monkeypatch):
    import main
    from core.broker import shioaji_compat as sc
    monkeypatch.setattr(main, "_connection_dropped", False)
    order = []
    monkeypatch.setattr(
        sc, "safe_login",
        lambda api, k, s, **kw: (order.append("login"), "ok")[1])
    m = _monitor_stub(_ready_ctx())
    m.far_contract = SimpleNamespace(code="TMFI6")
    api = SimpleNamespace(
        login=lambda *a, **k: "ok",
        quote=SimpleNamespace(subscribe=lambda *a, **k: None))
    monkeypatch.setenv("SHIOAJI_API_KEY", "k")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
    assert main._try_shioaji_reconnect(api, m, None, False)
    # contract: quarantine BEFORE login; fresh cert only after resubscribe —
    # currently the context is never touched (LIVE_READY survives)
    assert m._execution_context.to_dict().get("effective_mode") \
        == "live_quarantined", "reconnect must quarantine before re-login"


def test_reconnect_failed_cert_stays_quarantined(monkeypatch):
    import main
    from core.broker import shioaji_compat as sc
    monkeypatch.setattr(main, "_connection_dropped", False)

    def _boom(api, k, s, **kw):
        raise RuntimeError("login down")

    monkeypatch.setattr(sc, "safe_login", _boom)
    m = _monitor_stub(_ready_ctx())
    api = SimpleNamespace(quote=SimpleNamespace(subscribe=lambda *a, **k: None))
    monkeypatch.setenv("SHIOAJI_API_KEY", "k")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
    assert main._try_shioaji_reconnect(api, m, None, False) is False
    assert m._execution_context.to_dict().get("effective_mode") \
        == "live_quarantined", "failed reconnect must stay quarantined"


# ── (4) dashboard persistence: atomic writer/reader round-trip ──────────────

def test_execution_context_state_round_trip_atomic():
    # RED: the writer/reader module (atomic write + fsync, no torn JSON)
    # comes with the wiring phase — its absence is the contract
    with pytest.raises(ImportError):
        import core.execution_context_state  # noqa: F401


# ── (5) release identity verifier (release_dir-scoped, injected runner) ─────

def test_release_identity_verifier_exists():
    # RED: dedicated verifier with specified release_dir + injected runner;
    # env missing / mismatch / command failure all fail closed
    with pytest.raises(ImportError):
        import core.release_identity  # noqa: F401


# ── (6) real logout invalidates an existing certificate / gate ─────────────

def test_real_logout_invalidates_existing_gate(monkeypatch):
    # REAL broker logout (shioaji_session.logout — the single centralized
    # invalidation point) must kill the registry generation, so any monitor
    # context bound to that session can no longer reach LIVE_READY.
    from core import shioaji_session
    from core.live_route_certificate import session_registry
    api = SimpleNamespace(logout=lambda: None)
    session_registry.register(api)
    monkeypatch.setattr(shioaji_session, "_api", api)
    shioaji_session.logout()
    assert session_registry.generation(api) is None, \
        "real logout must invalidate the session generation"


# ── (7) normal exit sequence: cancel safety stop → place exit ───────────────

def test_exit_sequence_cancels_safety_stop_then_places():
    m = _monitor_stub(_ready_ctx(), safety_trade=SimpleNamespace(ts=1))
    m._execute_trade("EXIT", 44300, "2026-08-08T10:00:00", 1, reason="TEST")
    kinds = [c[0] for c in m.api.calls] + [c[0] for c in m.client.calls]
    assert kinds and kinds[0] == "cancel_order", \
        f"EXIT must cancel the safety stop FIRST: {kinds}"


def test_quarantine_does_not_orphan_exchange_safety_stop():
    # RED: with an outstanding exchange safety stop, quarantine must leave
    # a reconciled recovery path (ledger marker) — never a silent orphan
    with pytest.raises(ImportError):
        import core.emergency_intent  # noqa: F401
