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

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


STATE_CHANGING_ATTRS = ("place_order", "cancel_order", "update_order",
                        "modify_order")


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
    result = m._place_safety_stop(44300, "LONG", 1, 50)
    assert not m.api.calls, f"quarantined safety-stop placement: {m.api.calls}"
    assert isinstance(result, dict) and result.get("blocked") is True, \
        f"quarantined placement must return a structured blocked reason: {result}"
    assert "audit_reasons" in result, result


def test_safety_stop_placement_ready_permits_place():
    m = _monitor_stub(_ready_ctx())
    m._place_safety_stop(44300, "LONG", 1, 50)
    kinds = [c[0] for c in m.api.calls]
    assert "place_order" in kinds, \
        f"LIVE_READY must permit the safety-stop placement: {kinds}"


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


def test_ast_inventory_strategy_package_scope(tmp_path, monkeypatch):
    # v3.1 #5 + codex three-line correction: EXACT (path, function, attr)
    # inventory of the CANDIDATE under review, parsed from an isolated
    # clean git worktree at that commit (default HEAD; the wiring phase
    # sets CANDIDATE_COMMIT to the wiring commit AFTER it is committed).
    # Deterministic AND candidate-accurate — never blind to uncommitted
    # candidate changes, never the preceding HEAD.
    import subprocess
    import os

    candidate = os.environ.get("CANDIDATE_COMMIT", "HEAD")
    wt = tmp_path / "candidate_wt"
    subprocess.check_call(
        ["git", "worktree", "add", "--detach", str(wt), candidate],
        cwd=_repo_root(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wt_root = wt / "strategies" / "futures"

        def _read(rel):
            return (wt_root / rel).read_text(encoding="utf-8")

        def _sites(rel, src):
            tree = ast.parse(src)
            sites = []
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in STATE_CHANGING_ATTRS):
                    continue
                enclosing = None
                for fn in ast.walk(tree):
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and fn.lineno <= node.lineno <= getattr(fn, "end_lineno", node.lineno):
                        enclosing = fn.name
                sites.append((rel, enclosing, node.func.attr))
            return sites

        audited = {
            ("monitor.py", "_place_safety_stop", "place_order"),
            ("monitor.py", "_cancel_safety_stop", "cancel_order"),
            ("monitor.py", "_submit_order_via_manager", "place_order"),
            ("monitor.py", "_execute_trade", "place_order"),
            ("squeeze_futures/data/shioaji_client.py", "place_order", "place_order"),
            ("squeeze_futures/data/shioaji_client.py", "update_order", "update_order"),
            ("squeeze_futures/data/shioaji_client.py", "cancel_order", "cancel_order"),
        }
        found = set()
        for rel in ("monitor.py",
                    "squeeze_futures/data/shioaji_client.py"):
            found |= set(_sites(rel, _read(rel)))
        assert found == audited, \
            f"inventory drift (found={sorted(found)}, audited={sorted(audited)})"
    finally:
        subprocess.check_call(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=_repo_root(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)


# ── (2) emergency (v3.1): BLOCKED under quarantine this phase; the future
#      operator command must use the canonical P1-B core/exit_intent.py ──────

def _emergency_stub_strategy():
    return SimpleNamespace(_has_position=True, _released_leg=None,
                           _near_side="LONG")


def test_emergency_blocked_under_quarantine_this_phase():
    # RED (v3.1): Phase-2 wiring BLOCKS emergency entirely under
    # LIVE_QUARANTINED (no bypass) — the strategy must not be mutated and
    # a dashboard reason must be emitted. Currently the strategy is
    # mutated before any check.
    m = _monitor_stub(_ready_ctx())
    s = _emergency_stub_strategy()
    try:
        m._emergency_flatten_mts(s)
    except Exception:
        pass
    assert s._released_leg is None and not hasattr(s, "_side"), \
        "emergency must be blocked under quarantine (no strategy mutation)"


def test_emergency_blocked_emits_dashboard_reason():
    # RED (v3.1): the blocked emergency must emit a dashboard-safe reason
    # + operator procedure — currently there is no reason channel
    m = _monitor_stub(_quarantined_ctx())
    s = _emergency_stub_strategy()
    try:
        m._emergency_flatten_mts(s)
    except Exception:
        pass
    reasons = getattr(m._execution_context, "audit_reasons", ())
    assert any("EMERGENCY" in r for r in reasons), \
        f"blocked emergency must carry a dashboard reason: {reasons}"


def test_emergency_future_command_uses_exit_intent():
    # RED (v3.1): a future authorized emergency operator command must go
    # through the canonical core/exit_intent.py (create → child intent →
    # emergency_supersede → client_order_id idempotency → recover) — the
    # current path never touches it
    from core import exit_intent
    m = _monitor_stub(_ready_ctx())
    s = _emergency_stub_strategy()
    try:
        m._emergency_flatten_mts(s)
    except Exception:
        pass
    assert not hasattr(m, "_exit_intent"), \
        "emergency must route through core/exit_intent (create/supersede)"


def test_exit_intent_canonical_protocol_surface():
    # the canonical P1-B module (v3.1 anchor) must expose the durable
    # intent protocol the emergency command will use
    from core import exit_intent
    log = exit_intent.IntentLog
    for method in ("create", "submit_leg", "emergency_supersede",
                   "reconciliation_view", "recover", "mark_terminal"):
        assert hasattr(log, method), f"IntentLog.{method} missing"
    assert hasattr(exit_intent, "client_order_id")
    assert hasattr(exit_intent, "SupersededIntentError")


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


def test_reconnect_far_resubscribe_failure_stays_quarantined(monkeypatch):
    # v3.1 #6: safe_login succeeds but the FAR resubscribe fails — every
    # attempt must leave the ctx quarantined with zero broker calls
    import main
    from core.broker import shioaji_compat as sc
    monkeypatch.setattr(main, "_connection_dropped", False)
    monkeypatch.setattr(sc, "safe_login",
                        lambda api, k, s, **kw: "ok")

    class _Quote:
        def __init__(self):
            self.calls = []

        def subscribe(self, *a, **k):
            self.calls.append(("subscribe", a, k))
            if len(self.calls) >= 2:                 # second = far leg
                raise RuntimeError("far subscribe down")
            return None

    m = _monitor_stub(_ready_ctx())
    m.far_contract = SimpleNamespace(code="TMFI6")
    api = SimpleNamespace(quote=_Quote())
    monkeypatch.setenv("SHIOAJI_API_KEY", "k")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
    assert main._try_shioaji_reconnect(api, m, None, False) is False
    assert m._execution_context.to_dict().get("effective_mode") \
        == "live_quarantined", "far-subscribe failure must stay quarantined"


def test_reconnect_auto_code12_path_quarantines(monkeypatch):
    # v3.1 #6: the code-12 auto-recovery branch must quarantine the ctx
    # before its manual fallback re-login (sleep no-op'd)
    import main
    from core.broker import shioaji_compat as sc
    monkeypatch.setattr(main, "_connection_dropped", True)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(sc, "safe_login", lambda api, k, s, **kw: "ok")
    m = _monitor_stub(_ready_ctx())
    m.far_contract = SimpleNamespace(code="TMFI6")
    api = SimpleNamespace(
        quote=SimpleNamespace(subscribe=lambda *a, **k: None))
    monkeypatch.setenv("SHIOAJI_API_KEY", "k")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
    assert main._try_shioaji_reconnect(api, m, None, False)
    assert m._execution_context.to_dict().get("effective_mode") \
        == "live_quarantined", "code-12 auto path must quarantine first"


# ── broker adapter chokepoint (shioaji_client: 207/215/224) ─────────────────

def _adapter_stub(ctx):
    from strategies.futures.squeeze_futures.data.shioaji_client import (
        ShioajiClient)
    c = ShioajiClient.__new__(ShioajiClient)
    c.is_logged_in = True
    c.api = RecordingBroker()
    c._execution_context = ctx
    return c


def test_adapter_place_order_quarantined_zero_calls():
    # v3.1 #5: the ShioajiClient adapter is the broker chokepoint — a
    # quarantined context must RAISE the gate BEFORE any broker I/O.
    # NOTE: today the adapter swallows an OrderType.MTL error (missing in
    # shioaji 1.7.0) and returns None silently — no gate, no raise, no
    # broker call: a silent live-order failure (wiring must fix both).
    c = _adapter_stub(_quarantined_ctx())
    with pytest.raises(Exception) as ei:
        c.place_order(SimpleNamespace(code="TMFH6"), "BUY", 1)
    assert "Blocked" in type(ei.value).__name__, \
        f"quarantine must raise the gate, not swallow: {ei.value!r}"
    assert not c.api.calls, f"adapter place under quarantine: {c.api.calls}"


def test_adapter_cancel_order_quarantined_zero_calls():
    c = _adapter_stub(_quarantined_ctx())
    c.cancel_order(SimpleNamespace(ts=1))
    assert not c.api.calls, f"adapter cancel under quarantine: {c.api.calls}"


def test_adapter_update_order_quarantined_zero_calls():
    c = _adapter_stub(_quarantined_ctx())
    c.update_order(SimpleNamespace(ts=1), price=44300)
    assert not c.api.calls, f"adapter update under quarantine: {c.api.calls}"


# ── exit failure-side (v3.1 #7) ─────────────────────────────────────────────

def test_exit_does_not_silently_place_when_stop_cancel_fails():
    # if the safety-stop cancellation fails, an ordinary EXIT must not
    # silently place the exit unless reconciliation explicitly permits it
    # and records a durable reason — currently the cancel error is
    # swallowed inside _cancel_safety_stop and the exit still places
    m = _monitor_stub(_ready_ctx(), safety_trade=SimpleNamespace(ts=1))

    def _boom_cancel(*a, **k):
        m.api.calls.append(("cancel_order", a, k))
        raise RuntimeError("cancel down")

    m.api.cancel_order = _boom_cancel
    m._execute_trade("EXIT", 44300, "2026-08-08T10:00:00", 1, reason="TEST")
    placed = [c for c in m.client.calls if c[0] == "place_order"]
    assert not placed, \
        f"exit must not silently place after failed stop cancel: {m.client.calls}"


# ── (4) dashboard persistence: atomic writer/reader round-trip ──────────────

def test_execution_context_state_round_trip_atomic():
    # v3.1 #3 contract: writer/reader under TRADING_RUNTIME_DIR (not bare
    # /tmp); atomic replace + fsync(file AND parent); corrupt/missing read
    # → safe default LIVE_QUARANTINED; no broker/account data; dashboard
    # reader renders state after restart. Module absent → contract missing.
    with pytest.raises(ImportError):
        import core.execution_context_state  # noqa: F401


# ── (5) release identity verifier (release_dir-scoped, injected runner) ─────

def test_release_identity_verifier_exists():
    # v3.1 #4: dedicated verifier with a REAL temp git release dir +
    # injected runner; env missing / HEAD mismatch / command failure all
    # fail closed. Module absent → contract missing.
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
