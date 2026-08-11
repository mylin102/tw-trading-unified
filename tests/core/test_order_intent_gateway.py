"""RED tests: S0 OrderIntentGateway authorization boundary (in-memory).

E2E against a fake adapter: exact exit-only reaches adapter once;
entry/manual/generic/OCO zero calls; direct adapter / no authorization
rejected; PENDING_SUBMIT receipt missing -> PENDING_RECONCILE no retry;
normal LIVE entry retains behavior; paper unchanged.
"""

import time

import pytest

from core.mode_transition import (
    ExecutionContext,
    ModeTransitionState,
)
from core.order_management.order import OrderSide, OrderType
from core.order_management.order_manager import OrderManager


def _capability():
    return {
        "schema_version": 2,
        "reconciliation_id": "recon-abc123",
        "trade_id": "mts-20260811-085503",
        "snapshot_hash": "s" * 64,
        "attestation_hash": "a" * 64,
        "snapshot_captured_at": 1754991000000,
        "account_id_hash": "b" * 64,
        "session_id": "c" * 32,
        "config_hash": "d" * 64,
        "release_sha": "e" * 40,
        "allowed_orders": [
            {"symbol": "TMFH6", "side": "buy", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "sell", "remaining_qty": 1},
        ],
        "legs": [
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1,
             "avg_cost": 44909.0},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1,
             "avg_cost": 45052.0},
        ],
    }


def _exit_only_ctx():
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.RECONCILED_EXIT_ONLY.value,
        live_order_allowed=False,
        exit_only_capability=_capability(),
    )


def _live_ctx():
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
    )


def _bbo_slots(now=None):
    ts = now if now is not None else time.time()
    return {
        "TMF": {"bid": 44900.0, "ask": 44910.0, "bidask_at": ts},
        "TMF_FAR": {"bid": 45040.0, "ask": 45060.0, "bidask_at": ts},
    }


class _FakeAdapter:
    """Mirrors the real adapter: gateway-registry verification + record."""

    def __init__(self, registry=None):
        self._gateway_registry = registry
        self.calls = []

    def place_order_object(self, order):
        reg = getattr(self, "_gateway_registry", None)
        if reg is not None and not reg.verify_pending_submission():
            raise RuntimeError("ADAPTER_GATEWAY_AUTHORIZATION_MISSING")
        self.calls.append({
            "order_id": order.order_id, "symbol": order.symbol,
            "side": getattr(order.side, "value", order.side),
            "quantity": order.quantity, "strategy": order.strategy,
            "reconciliation_id": getattr(order, "reconciliation_id", None),
        })
        order.exchange_order_id = f"BROKER-{order.order_id}"
        from types import SimpleNamespace
        return SimpleNamespace(exchange_order_id=order.exchange_order_id,
                               seqno="1")

    def get_contract(self, symbol):
        from types import SimpleNamespace
        return SimpleNamespace(code=symbol)

    def place_order(self, *args, **kwargs):
        raise AssertionError("raw place_order must never be called")

    def update_order(self, *args, **kwargs):
        raise AssertionError("update must never be called")

    def cancel_order(self, *args, **kwargs):
        raise AssertionError("cancel must never be called")


class _NoReceiptAdapter(_FakeAdapter):
    """Submits but returns no broker receipt identity."""

    def place_order_object(self, order):
        self.calls.append({"order_id": order.order_id})
        return None


# ── unit: authorization ───────────────────────────────────────────────────

def test_authorization_single_use_expiry_and_restart():
    from core.order_intent_gateway import (
        GatewayAuthorizationRegistry,
    )

    reg = GatewayAuthorizationRegistry(process_epoch="e1")
    auth = reg.issue("ORD-1", 1, "g1")
    assert reg.verify(auth) is True
    assert reg.verify_pending_submission() is True
    assert reg.consume(auth) is True
    assert reg.verify(auth) is False          # single-use
    assert reg.verify_pending_submission() is False

    auth2 = reg.issue("ORD-2", 1, "g1", expiry_ts=time.time() - 1)
    assert reg.verify(auth2) is False          # expired

    # restart: fresh registry -> old authorization dies
    reg2 = GatewayAuthorizationRegistry(process_epoch="e2")
    assert reg2.verify(auth) is False


def test_authorization_fingerprint_only():
    from core.order_intent_gateway import GatewayAuthorizationRegistry

    reg = GatewayAuthorizationRegistry(process_epoch="e1")
    auth = reg.issue("ORD-1", 1, "g1")
    fp = auth.fingerprint
    assert isinstance(fp, str) and len(fp) == 64
    # fingerprint is a hash, not the material
    assert "ORD-1" not in fp


# ── unit: policy matrix ───────────────────────────────────────────────────

def test_policy_matrix_live_and_exit_only():
    from core.order_intent_gateway import OrderIntentGateway

    gw = OrderIntentGateway(process_epoch="e1")

    # paper pass-through
    ok, binding, reason = gw.authorize_intent(
        action="BUY_NEAR_SELL_FAR", strategy="MTS_ENTRY",
        authority={"live": False})
    assert ok is True and binding is None and reason is None

    # non-ready live mode blocked
    ok, _, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_EXIT",
        authority={"live": True, "mode": "live_quarantined",
                   "live_order_allowed": False})
    assert ok is False and reason == "LIVE_ORDER_AUTHORIZATION_FAILED"

    # live_ready entry retains behavior
    ok, _, reason = gw.authorize_intent(
        action="BUY_NEAR_SELL_FAR", strategy="MTS_ENTRY",
        authority={"live": True, "mode": "live_ready",
                   "live_order_allowed": True, "position_has_position": True})
    assert ok is True and reason is None

    # live_ready exit with flat position blocked (merged FLAT check)
    ok, _, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_EXIT",
        authority={"live": True, "mode": "live_ready",
                   "live_order_allowed": True, "position_has_position": False})
    assert ok is False and reason == "EXIT_FLAT_BLOCKED"

    # OCO explicitly disabled at the gateway
    ok, _, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_RELEASE_OCO",
        authority={"live": True, "mode": "live_ready",
                   "live_order_allowed": True, "position_has_position": True})
    assert ok is False and reason == "GATEWAY_OCO_DISABLED"


def test_policy_exit_only_only_capability_bound():
    from core.order_intent_gateway import OrderIntentGateway

    gw = OrderIntentGateway(process_epoch="e1")
    cap = _capability()
    base = {
        "live": True, "mode": "reconciled_exit_only",
        "live_order_allowed": False, "capability": cap,
        "hydrated_position": {"trade_id": "mts-20260811-085503"},
        "strategy_reconciliation_id": cap["reconciliation_id"],
        "near_code": "TMFH6", "far_code": "TMFI6",
        "bbo_slots": _bbo_slots(),
    }

    # entry denied
    ok, _, reason = gw.authorize_intent(
        action="BUY_NEAR_SELL_FAR", strategy="MTS_ENTRY", authority=base)
    assert ok is False and reason == "EXIT_ONLY_ENTRY_BLOCKED"

    # generic strategy denied
    ok, _, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_MANUAL", authority=base)
    assert ok is False and reason == "EXIT_ONLY_STRATEGY_BLOCKED"

    # unbound strategy denied
    unbound = dict(base, strategy_reconciliation_id="other")
    ok, _, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_EXIT", authority=unbound)
    assert ok is False and reason == "EXIT_ONLY_STRATEGY_BLOCKED"

    # missing position denied
    nopos = dict(base, hydrated_position=None)
    ok, _, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_EXIT", authority=nopos)
    assert ok is False and reason == "EXIT_ONLY_POSITION_MISSING"

    # missing BBO denied (zero order)
    nobbo = dict(base, bbo_slots={})
    ok, _, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_EXIT", authority=nobbo)
    assert ok is False and reason == "BBO_MISSING"

    # contract code mismatch denied
    badcode = dict(base, near_code="TMFX6")
    ok, _, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_EXIT", authority=badcode)
    assert ok is False and reason == "BBO_CODE_MISMATCH"

    # exact capability-bound exit allowed with binding
    ok, binding, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_EXIT", authority=base)
    assert ok is True and reason is None
    assert binding["bbo_hash"]

    ok, binding, reason = gw.authorize_intent(
        action="EXIT", strategy="MTS_RELEASE", authority=base)
    assert ok is True and reason is None


# ── unit: submit / receipt rule ───────────────────────────────────────────

def test_submit_receipt_missing_pending_reconcile_no_retry():
    from core.order_intent_gateway import OrderIntentGateway

    gw = OrderIntentGateway(process_epoch="e1")

    def _no_receipt(order):
        return True  # "submitted" but no exchange_order_id attached

    ok, reason = gw.submit_with_authorization(
        _order("ORD-1", "MTS_EXIT"), mode="live",
        submit_callable=_no_receipt)
    assert ok is False
    assert reason == "GATEWAY_RECEIPT_MISSING_RECONCILE"
    assert gw.intent_state("ORD-1") == "PENDING_RECONCILE"

    # never auto-resubmit
    ok, reason = gw.submit_with_authorization(
        _order("ORD-1", "MTS_EXIT"), mode="live",
        submit_callable=_no_receipt)
    assert ok is False and reason == "GATEWAY_RECONCILE_REQUIRED"


def test_submit_paper_unchanged_no_authorization():
    from core.order_intent_gateway import OrderIntentGateway

    gw = OrderIntentGateway(process_epoch="e1")
    calls = []

    def _paper(order, exchange_ordno=None):
        calls.append(exchange_ordno)
        return True

    ok, reason = gw.submit_with_authorization(
        _order("ORD-P1", "MTS_ENTRY"), mode="paper",
        exchange_ordno="PAPER-ORD-P1", submit_callable=_paper)
    assert ok is True and reason == ""
    assert calls == ["PAPER-ORD-P1"]
    # no authorization was issued (pending stays empty)
    assert gw.registry.verify_pending_submission() is False


def test_direct_adapter_without_authorization_rejected():
    from core.order_intent_gateway import GatewayAuthorizationRegistry

    reg = GatewayAuthorizationRegistry(process_epoch="e1")
    adapter = _FakeAdapter(registry=reg)

    with pytest.raises(RuntimeError) as exc:
        adapter.place_order_object(_order("ORD-D1", "MTS_EXIT"))
    assert "ADAPTER_GATEWAY_AUTHORIZATION_MISSING" in str(exc.value)
    assert adapter.calls == []


def _order(order_id, strategy, side=OrderSide.BUY, symbol="TMFH6",
           reconciliation_id=None):
    from core.order_management.order import Order
    return Order(
        symbol=symbol, side=side, order_type=OrderType.MKP,
        quantity=1, strategy=strategy, order_id=order_id,
        reconciliation_id=reconciliation_id)


# ── E2E: through the monitor signal path ──────────────────────────────────

def _monitor(ctx, adapter=None, mode="live", *, dry_run=False,
             live_trading=True):
    from types import SimpleNamespace
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._execution_context = ctx
    monitor.market_data = dict(_bbo_slots())
    monitor.ticker = "TMF"
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor.live_trading = live_trading
    monitor.dry_run = dry_run
    monitor._exit_only_position = None
    monitor._exit_only_decision_binding = None
    monitor._pending_lifecycle_orders = {}
    monitor._claimed_execution_keys = set()
    monitor.paper_fill_sim = None
    monitor.cfg = {"mts": {}}
    monitor.EXEC = {}
    events = []
    monitor._append_mts_event = lambda t, **k: events.append((t, k))
    monitor._save_orders_file_wrapper = lambda: True
    if adapter is None:
        adapter = _FakeAdapter()
    monitor.api = adapter
    monitor.order_mgr = OrderManager(
        mode=mode, broker_adapter=adapter, execution_context=ctx)
    return monitor, events


def _bound_strategy(rid="recon-abc123", trade_id="mts-20260811-085503"):
    from types import SimpleNamespace
    return SimpleNamespace(
        _trade_id=trade_id, _reconciliation_id=rid,
        _near_side="SHORT", _far_side="LONG",
        _near_qty=1, _far_qty=1, _has_position=True)


def test_e2e_exit_only_exact_cap_bound_orders_reach_adapter_once(
        monkeypatch):
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_exit_only_ctx())
    monitor._hydrate_exit_only_position()
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    signal = SimpleNamespace(action="EXIT", reason="COMBINED_EXIT")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    calls = monitor.api.calls
    # exactly the two capability-bound closing orders, once each
    assert len(calls) == 2
    by_symbol = {c["symbol"]: c for c in calls}
    assert set(by_symbol) == {"TMFH6", "TMFI6"}
    assert by_symbol["TMFH6"]["side"] == "buy"
    assert by_symbol["TMFI6"]["side"] == "sell"
    assert by_symbol["TMFH6"]["strategy"] == "MTS_EXIT"
    assert by_symbol["TMFH6"]["reconciliation_id"] == "recon-abc123"
    assert by_symbol["TMFI6"]["reconciliation_id"] == "recon-abc123"
    # one call per order (no duplicate submits)
    assert [c["order_id"] for c in calls].count(calls[0]["order_id"]) == 1


def test_e2e_entry_and_oco_zero_calls(monkeypatch):
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_exit_only_ctx())
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    signal = SimpleNamespace(action="BUY_NEAR_SELL_FAR", reason="ENTRY")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    assert monitor.api.calls == []
    blocked = [e for e in events if e[0] == "ORDER_INTENT_BLOCKED"]
    assert blocked and blocked[0][1]["reason"] == "EXIT_ONLY_ENTRY_BLOCKED"


def test_e2e_live_entry_retains_behavior(monkeypatch):
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_live_ctx())
    monitor._hydrate_exit_only_position()  # no-op (not exit-only)
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    signal = SimpleNamespace(action="BUY_NEAR_SELL_FAR", reason="ENTRY")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    # the entry path submits to the adapter as before (authorization flows)
    assert len(monitor.api.calls) == 2
    assert {c["strategy"] for c in monitor.api.calls} == {"MTS_ENTRY"}


def test_e2e_paper_unchanged(monkeypatch):
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(
        _live_ctx(), mode="paper", dry_run=True, live_trading=False)
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    signal = SimpleNamespace(action="BUY_NEAR_SELL_FAR", reason="ENTRY")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    # paper never touches the broker adapter
    assert monitor.api.calls == []
