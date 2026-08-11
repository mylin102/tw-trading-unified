"""RED tests: S0 OrderIntentGateway authorization boundary (in-memory).

E2E against a fake adapter: exact exit-only reaches adapter once;
entry/manual/generic/OCO zero calls; direct adapter / no authorization
rejected; PENDING_SUBMIT receipt missing -> PENDING_RECONCILE no retry;
normal LIVE entry retains behavior; paper unchanged.

S0 audit fixes: registry injected into the actual broker_adapter (real
monitor->OrderManager->ShioajiClient chain); authorization bound to the
exact order; restart/new gateway replays durable intents and never
resubmits; live quarantine explicitly denied (never paper); OCO direct
paths blocked in EXIT_ONLY; exit-intent failed leg never SUBMITTED.
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


def _ctx(mode, live_order_allowed=False, cap=None):
    return ExecutionContext(
        requested_mode="live",
        effective_mode=mode,
        live_order_allowed=live_order_allowed,
        exit_only_capability=cap,
    )


def _exit_only_ctx():
    return _ctx(ModeTransitionState.RECONCILED_EXIT_ONLY.value,
                live_order_allowed=False, cap=_capability())


def _live_ctx():
    return _ctx(ModeTransitionState.LIVE_READY.value, live_order_allowed=True)


def _quarantined_ctx():
    return _ctx(ModeTransitionState.LIVE_QUARANTINED.value,
                live_order_allowed=False)


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
        if reg is not None and not reg.verify_pending_submission(order):
            raise RuntimeError("ADAPTER_GATEWAY_AUTHORIZATION_MISSING")
        self.calls.append({
            "order_id": order.order_id, "symbol": order.symbol,
            "side": getattr(order.side, "value", order.side),
            "quantity": order.quantity, "strategy": order.strategy,
            "reconciliation_id": getattr(order, "reconciliation_id", None),
        })
        order.exchange_order_id = f"BROKER-{order.order_id}"
        from types import SimpleNamespace
        return SimpleNamespace(id=order.exchange_order_id, seqno="1")

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
    # pending verification requires the exact order object
    assert reg.verify_pending_submission(_order("ORD-1", "MTS_EXIT")) is True
    assert reg.verify_pending_submission() is False
    assert reg.consume(auth) is True
    assert reg.verify(auth) is False          # single-use
    assert reg.verify_pending_submission(_order("ORD-1", "MTS_EXIT")) is False

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


def test_verify_pending_binds_exact_order():
    """[audit #2] a different order during a valid pending window rejects."""
    from core.order_intent_gateway import GatewayAuthorizationRegistry

    reg = GatewayAuthorizationRegistry(process_epoch="e1")
    reg.issue("ORD-1", 1, "g1")
    assert reg.verify_pending_submission(_order("ORD-1", "MTS_EXIT")) is True
    assert reg.verify_pending_submission(_order("ORD-2", "MTS_EXIT")) is False
    # raw place_order passes no order -> must be rejected
    assert reg.verify_pending_submission() is False
    assert reg.verify_pending_submission(None) is False


# ── unit: policy matrix ───────────────────────────────────────────────────

def test_policy_matrix_live_and_exit_only():
    from core.order_intent_gateway import OrderIntentGateway

    gw = OrderIntentGateway(process_epoch="e1")

    # paper pass-through
    ok, binding, reason = gw.authorize_intent(
        action="BUY_NEAR_SELL_FAR", strategy="MTS_ENTRY",
        authority={"live": False})
    assert ok is True and binding is None and reason is None

    # live quarantined explicitly denied — never paper-pass-through
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
    bbo = {
        "near": _bbo_slots()["TMF"],
        "far": _bbo_slots()["TMF_FAR"],
    }
    base = {
        "live": True, "mode": "reconciled_exit_only",
        "live_order_allowed": False, "capability": cap,
        "hydrated_position": {"trade_id": "mts-20260811-085503"},
        "strategy_reconciliation_id": cap["reconciliation_id"],
        "near_code": "TMFH6", "far_code": "TMFI6",
        "bbo_slots": bbo,
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


def test_restart_new_gateway_cannot_resubmit():
    """[audit #3] durable intents replay across restart; PENDING_RECONCILE
    and crash-mid-submit (PENDING_SUBMIT) are never resubmitted."""
    from core.order_intent_gateway import OrderIntentGateway

    def _no_receipt(order):
        return True

    records = []
    gw1 = OrderIntentGateway(process_epoch="e1", record_cb=records.append)
    ok, reason = gw1.submit_with_authorization(
        _order("ORD-1", "MTS_EXIT"), mode="live",
        submit_callable=_no_receipt)
    assert ok is False and reason == "GATEWAY_RECEIPT_MISSING_RECONCILE"
    assert records, "durable record_cb must have fired"
    durable = gw1.durable_view()
    assert durable["ORD-1"]["state"] == "PENDING_RECONCILE"

    # restart: fresh gateway replays the durable ledger -> refuses resubmit
    gw2 = OrderIntentGateway(process_epoch="e2", durable_intents=durable)
    ok, reason = gw2.submit_with_authorization(
        _order("ORD-1", "MTS_EXIT"), mode="live",
        submit_callable=_no_receipt)
    assert ok is False and reason == "GATEWAY_RECONCILE_REQUIRED"

    # crash mid-submit: durable PENDING_SUBMIT -> restart must NOT resubmit
    gw3 = OrderIntentGateway(process_epoch="e3", durable_intents={
        "ORD-2": {"state": "PENDING_SUBMIT", "execution_attempt": 0,
                  "strategy": "MTS_EXIT", "reconciliation_id": None,
                  "session_generation": ""}})
    ok, reason = gw3.submit_with_authorization(
        _order("ORD-2", "MTS_EXIT"), mode="live",
        submit_callable=_no_receipt)
    assert ok is False and reason == "GATEWAY_RECONCILE_REQUIRED"


def test_submit_rejected_local_is_terminal():
    """A local rejection marks REJECTED (terminal) and never resubmits."""
    from core.order_intent_gateway import OrderIntentGateway

    gw = OrderIntentGateway(process_epoch="e1")

    def _reject(order):
        return False

    ok, reason = gw.submit_with_authorization(
        _order("ORD-R", "MTS_EXIT"), mode="live",
        submit_callable=_reject)
    assert ok is False and reason == "SUBMIT_REJECTED"
    assert gw.intent_state("ORD-R") == "REJECTED"

    ok, reason = gw.submit_with_authorization(
        _order("ORD-R", "MTS_EXIT"), mode="live",
        submit_callable=_reject)
    assert ok is False and reason == "GATEWAY_RECONCILE_REQUIRED"


def test_persist_failure_blocks_adapter():
    """[audit round2 #1] a failed durable PENDING_SUBMIT must abort BEFORE
    the adapter: no authorization issued, no submit_callable invocation."""
    from core.order_intent_gateway import GatewayIntentPersistFailed
    from core.order_intent_gateway import OrderIntentGateway

    def _failing_record(view):
        raise GatewayIntentPersistFailed("disk full")

    gw = OrderIntentGateway(process_epoch="e1",
                            record_cb=_failing_record)
    calls = []

    def _submit(order):
        calls.append(order.order_id)
        order.exchange_order_id = f"BROKER-{order.order_id}"
        return True

    ok, reason = gw.submit_with_authorization(
        _order("ORD-PF", "MTS_EXIT"), mode="live",
        submit_callable=_submit)
    assert ok is False
    assert reason == "GATEWAY_INTENT_PERSIST_FAILED"
    assert calls == []                       # zero adapter/submit calls
    assert gw.registry.verify_pending_submission(
        _order("ORD-PF", "MTS_EXIT")) is False  # no auth issued


def test_direct_place_order_rejected_during_pending():
    """[audit round2 #2] raw place_order (no order object) must always be
    rejected while a live gateway registry is injected."""
    from core.order_intent_gateway import GatewayAuthorizationRegistry
    from strategies.futures.squeeze_futures.data.shioaji_client import (
        AdapterOrderError,
        ShioajiClient,
    )

    reg = GatewayAuthorizationRegistry(process_epoch="e1")
    reg.issue("ORD-1", 1, "g1")          # valid pending order-object window
    client = ShioajiClient.__new__(ShioajiClient)
    client._gateway_registry = reg
    client._execution_context = _live_ctx()

    with pytest.raises(AdapterOrderError) as exc:
        client.place_order("TMFH6", "Buy", 1)
    assert exc.value.code == "ADAPTER_GATEWAY_AUTHORIZATION_MISSING"


def test_exit_intent_ledger_broker_order_id(tmp_path):
    """[audit round2 #3] the ledger persists the canonical BROKER identity
    in broker_order_id — never the local order_id."""
    from core.exit_intent import IntentLog

    log = IntentLog(log_dir=str(tmp_path))
    iid = log.create("mts-t1", "COMBINED_EXIT")
    log.submit_leg(
        iid, "NEAR", order_mgr=None,
        submit_fn=lambda cid, leg: {
            "order_id": "LOCAL-1",
            "broker_order_id": "BROKER-1",
        })
    rec = log.get(iid)
    assert rec["legs"]["NEAR"]["broker_order_id"] == "BROKER-1"
    assert rec["legs"]["NEAR"]["broker_order_id"] != "LOCAL-1"


def test_submit_paper_unchanged_no_authorization():
    from core.order_intent_gateway import OrderIntentGateway

    gw = OrderIntentGateway(process_epoch="e1")
    calls = []

    def _paper(order, exchange_ordno=None):
        calls.append(exchange_ordno)
        return True

    ok, payload = gw.submit_with_authorization(
        _order("ORD-P1", "MTS_ENTRY"), mode="paper",
        exchange_ordno="PAPER-ORD-P1", submit_callable=_paper)
    assert ok is True and isinstance(payload, dict)
    assert payload["order_id"] == "ORD-P1"
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


def test_wrong_order_during_valid_pending_rejected():
    """[audit #2 E2E] ORD-2 during ORD-1's pending window is rejected."""
    from core.order_intent_gateway import (
        GatewayAuthorizationRegistry,
        OrderIntentGateway,
    )

    reg = GatewayAuthorizationRegistry(process_epoch="e1")
    gw = OrderIntentGateway(registry=reg, process_epoch="e1")
    adapter = _FakeAdapter(registry=reg)

    # open a pending authorization for ORD-1 without submitting yet
    reg.issue("ORD-1", 1, "g1")

    # a direct adapter call for a DIFFERENT order must fail
    with pytest.raises(RuntimeError) as exc:
        adapter.place_order_object(_order("ORD-2", "MTS_EXIT"))
    assert "ADAPTER_GATEWAY_AUTHORIZATION_MISSING" in str(exc.value)

    # the bound order passes
    adapter.place_order_object(_order("ORD-1", "MTS_EXIT"))
    assert [c["order_id"] for c in adapter.calls] == ["ORD-1"]


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
    monitor._mts_pending_fills = {}
    monitor._ledger_projection = SimpleNamespace(
        sync_from_ledger=lambda: None,
        snapshot=lambda: SimpleNamespace(status="FLAT"))
    monitor._ledger_projection_sync_ts = 0.0
    monitor.paper_fill_sim = None
    monitor.cfg = {"mts": {}}
    monitor.EXEC = {}
    events = []
    monitor._append_mts_event = lambda t, **k: events.append((t, k))
    monitor._save_orders_file_wrapper = lambda: True
    monitor._persist_execution_context = lambda: None  # isolate ctx file
    monitor._record_gateway_intent = lambda dv: None    # isolate durable ledger
    if adapter is None:
        adapter = _FakeAdapter()
    monitor.api = adapter
    monitor.order_mgr = OrderManager(
        mode=mode, broker_adapter=adapter, execution_context=ctx)
    monitor._gateway()  # init-equivalent: registry injected before any path
    return monitor, events


def _bound_strategy(rid="recon-abc123", trade_id="mts-20260811-085503"):
    from types import SimpleNamespace
    return SimpleNamespace(
        _trade_id=trade_id, _reconciliation_id=rid,
        _near_side="SHORT", _far_side="LONG",
        _near_qty=1, _far_qty=1, _has_position=True)


def test_e2e_exit_only_exact_cap_bound_orders_reach_adapter_once(
        monkeypatch, tmp_path):
    import json as _json
    import os
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_exit_only_ctx())
    monitor._hydrate_exit_only_position()
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    monkeypatch.setattr(monitor_mod, "_mts_intent_log_dir",
                        lambda: str(tmp_path))
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

    # [audit round2 #3] the exit-intent ledger persisted the canonical
    # BROKER identity in broker_order_id (never the local id)
    rows = []
    with open(os.path.join(str(tmp_path), "mts_exit_intent.jsonl"),
              encoding="utf-8") as f:
        for line in f:
            rows.append(_json.loads(line))
    rec = [r for r in rows
           if r.get("trade_id") == "mts-20260811-085503"][-1]
    near_bid = rec["legs"]["NEAR"]["broker_order_id"]
    far_bid = rec["legs"]["FAR"]["broker_order_id"]
    assert near_bid.startswith("BROKER-") and far_bid.startswith("BROKER-")
    assert near_bid != rec["legs"]["NEAR"].get("client_order_id")
    assert far_bid != rec["legs"]["FAR"].get("client_order_id")


def test_gateway_registry_injected_before_any_signal():
    """[repair A] the registry is injected at init (before any MTS order
    path): a direct adapter call is rejected even before the first signal."""
    monitor, events = _monitor(_live_ctx())
    assert monitor.api._gateway_registry is not None
    with pytest.raises(RuntimeError) as exc:
        monitor.api.place_order_object(_order("ORD-PRE", "MTS_ENTRY"))
    assert "ADAPTER_GATEWAY_AUTHORIZATION_MISSING" in str(exc.value)


def _flag_monitor(ctx, flag):
    import json
    import os
    import tempfile

    monitor, events = _monitor(ctx)
    p = tempfile.mkdtemp(prefix="s0_flag_")
    flag_path = os.path.join(p, "flag.json")
    with open(flag_path, "w", encoding="utf-8") as f:
        json.dump(flag, f)
    monitor.manual_trade_flag_path = flag_path
    monitor._processed_flag_ids = set()
    monitor._flag_retry_count = 0
    monitor._manual_trade_status = ""
    monitor._current_flag_id = ""
    monitor._registry = {"tmf_spread": _bound_strategy()}
    return monitor, events


def test_e2e_manual_entry_blocked_in_exit_only():
    """[repair B] MTS_MANUAL in EXIT_ONLY is blocked BEFORE any Order
    construction/submission — zero submit, zero adapter call."""
    import time

    monitor, events = _flag_monitor(_exit_only_ctx(), {
        "action": "spread", "side": "SELL_NEAR_BUY_FAR",
        "near_close": 44905, "far_close": 45038.5,
        "created_at": time.time(), "command_id": "cmd-1"})
    monitor._process_manual_trade_flag()
    assert monitor.api.calls == []
    blocked = [e for e in events if e[0] == "ORDER_INTENT_BLOCKED"]
    assert blocked and "STRATEGY_BLOCKED" in blocked[0][1]["reason"]


def test_e2e_manual_entry_normal_live_submits(monkeypatch):
    """[repair B] manual entry in normal live routes through the gateway
    and reaches the adapter (ORDER_SUBMITTED only after the receipt)."""
    import time
    from pathlib import Path
    import strategies.futures.monitor as monitor_mod

    monitor, events = _flag_monitor(_live_ctx(), {
        "action": "spread", "side": "SELL_NEAR_BUY_FAR",
        "near_close": 44905, "far_close": 45038.5,
        "created_at": time.time(), "command_id": "cmd-2"})
    monitor._registry["tmf_spread"]._has_position = False
    monkeypatch.setattr(monitor_mod, "_mts_position_state_path",
                        lambda: Path("/tmp/test_s0_no_state.json"))
    monitor._process_manual_trade_flag()
    assert len(monitor.api.calls) == 2
    assert {c["strategy"] for c in monitor.api.calls} == {"MTS_MANUAL"}
    submitted = [e for e in events if e[0] == "ORDER_SUBMITTED"]
    assert len(submitted) == 2


def test_e2e_emergency_close_all_blocked_in_exit_only():
    """[repair B] emergency close_all in EXIT_ONLY is explicitly blocked —
    zero submit, zero adapter call."""
    import time

    monitor, events = _flag_monitor(_exit_only_ctx(), {
        "action": "close_all", "created_at": time.time(),
        "command_id": "cmd-close"})
    monitor._process_manual_trade_flag()
    assert monitor.api.calls == []
    blocked = [e for e in events if e[0] == "ORDER_INTENT_BLOCKED"]
    assert blocked and "EXIT_ONLY_EMERGENCY_BLOCKED" in blocked[0][1]["reason"]


def test_e2e_release_submitted_event_only_after_receipt(monkeypatch):
    """[repair C] RELEASE emits ORDER_INTENT_CREATED before the submit and
    ORDER_SUBMITTED only after the canonical broker receipt."""
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_exit_only_ctx())
    monitor._hydrate_exit_only_position()
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    signal = SimpleNamespace(action="PARTIAL_EXIT", reason="RELEASE_NEAR")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    assert len(monitor.api.calls) == 1
    assert monitor.api.calls[0]["strategy"] == "MTS_RELEASE"
    created = [e for e in events if e[0] == "ORDER_INTENT_CREATED"]
    submitted = [e for e in events if e[0] == "ORDER_SUBMITTED"]
    assert len(created) == 1 and len(submitted) == 1
    assert events.index(submitted[0]) > events.index(created[0])


def test_e2e_release_failure_no_false_submitted(monkeypatch):
    """[repair C] a failed release submit emits ORDER_INTENT_BLOCKED —
    never a false ORDER_SUBMITTED."""
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_exit_only_ctx())
    monitor._hydrate_exit_only_position()
    monitor.order_mgr.submit = lambda order: False
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    signal = SimpleNamespace(action="PARTIAL_EXIT", reason="RELEASE_NEAR")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    assert [e[0] for e in events].count("ORDER_SUBMITTED") == 0
    assert any(e[0] == "ORDER_INTENT_BLOCKED" for e in events)
    assert monitor.api.calls == []


def test_e2e_persist_failure_zero_adapter_calls(monkeypatch):
    """[audit round2 #1 E2E] persistence failure -> typed
    GATEWAY_INTENT_PERSIST_FAILED -> zero adapter calls (the durable
    PENDING_SUBMIT was not confirmed)."""
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod
    from core.order_intent_gateway import GatewayIntentPersistFailed

    monitor, events = _monitor(_exit_only_ctx())
    monitor._hydrate_exit_only_position()

    def _fail_persist(view):
        raise GatewayIntentPersistFailed("disk full")

    monitor._record_gateway_intent = _fail_persist
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    signal = SimpleNamespace(action="EXIT", reason="COMBINED_EXIT")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    assert monitor.api.calls == []
    failed = [e for e in events if e[0] == "ORDER_INTENT_SUBMIT_FAILED"]
    assert failed and "GATEWAY_INTENT_PERSIST_FAILED" in failed[0][1]["reason"]


def test_combined_exit_gateway_failure_quarantines_no_second_leg(
        monkeypatch, tmp_path):
    """[audit round2 #4] a failed NEAR leg (GatewaySubmitError) is caught by
    the combined-exit branch: typed event, quarantine/reconcile, zero
    second-leg (FAR) submission."""
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_exit_only_ctx())
    monitor._hydrate_exit_only_position()
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    monkeypatch.setattr(monitor_mod, "_mts_intent_log_dir",
                        lambda: str(tmp_path))
    submitted = []

    def _sub(order):
        submitted.append(order.symbol)
        if order.symbol == "TMFH6":       # near leg rejected locally
            return False
        order.exchange_order_id = f"BROKER-{order.order_id}"
        return True

    monitor.order_mgr.submit = _sub
    signal = SimpleNamespace(action="EXIT", reason="COMBINED_EXIT")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    # near leg attempted exactly once; the FAR leg never submitted
    assert submitted == ["TMFH6"]
    # [repair D] the failure atomically forces LIVE_QUARANTINED and persists
    ctx = monitor._execution_context
    assert ctx.effective_mode == "live_quarantined"
    assert ctx.live_order_allowed is False
    reasons = list(ctx.audit_reasons or ())
    assert any("MTS_EXIT_LEG_FAILED:NEAR" in r for r in reasons)
    quarantined = [e for e in events if e[0] == "EXIT_ONLY_QUARANTINED"]
    assert quarantined and "NEAR:" in quarantined[0][1]["reason"]


def test_combined_exit_far_failure_no_retry(monkeypatch, tmp_path):
    """[audit round2 #4b] a failed FAR leg is attempted exactly once and
    never retried — typed event, quarantine/reconcile."""
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_exit_only_ctx())
    monitor._hydrate_exit_only_position()
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    monkeypatch.setattr(monitor_mod, "_mts_intent_log_dir",
                        lambda: str(tmp_path))
    submitted = []

    def _sub(order):
        submitted.append(order.symbol)
        if order.symbol == "TMFI6":       # far leg rejected locally
            return False
        order.exchange_order_id = f"BROKER-{order.order_id}"
        return True

    monitor.order_mgr.submit = _sub
    signal = SimpleNamespace(action="EXIT", reason="COMBINED_EXIT")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    assert submitted == ["TMFH6", "TMFI6"]
    assert submitted.count("TMFI6") == 1   # no retry
    failed = [e for e in events if e[0] == "ORDER_INTENT_SUBMIT_FAILED"]
    assert failed and "FAR:SUBMIT_REJECTED" in failed[0][1]["reason"]


def test_oco_blocked_all_modes():
    """[audit round2 #5] direct OCO paths fail closed in ALL modes (normal
    live included) — zero Order construction, zero submit until S9."""
    from types import SimpleNamespace

    monitor, events = _monitor(_live_ctx())     # normal live
    submitted = []
    monitor.paper_fill_sim = SimpleNamespace(register=lambda o: None)

    def _recording_submit(order):
        submitted.append(order.order_id)
        return True

    monitor.order_mgr.submit = _recording_submit
    monitor._reconcile_paper_oco_orders_from_state()
    monitor._reconcile_paper_oco_orders(_bound_strategy())

    assert submitted == []
    assert monitor.api.calls == []


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
    from pathlib import Path
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_live_ctx())
    monitor._hydrate_exit_only_position()  # no-op (not exit-only)
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    monkeypatch.setattr(monitor_mod, "_mts_position_state_path",
                        lambda: Path("/tmp/test_s0_no_state.json"))
    signal = SimpleNamespace(action="BUY_NEAR_SELL_FAR", reason="ENTRY")
    strat = _bound_strategy()
    strat._has_position = False

    monitor._submit_mts_order_signal(
        signal, strat, {}, __import__("datetime").datetime.now())

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


def test_e2e_quarantine_zero_adapter_calls(monkeypatch):
    """[audit #4] live quarantine is explicitly denied by the gateway,
    independent of the OrderManager gate (permissive submit stub)."""
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod

    monitor, events = _monitor(_quarantined_ctx())
    calls = []

    def _permissive(order):
        calls.append(order.order_id)
        return True

    monitor.order_mgr.submit = _permissive  # bypass the OrderManager gate
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    signal = SimpleNamespace(action="BUY_NEAR_SELL_FAR", reason="ENTRY")

    monitor._submit_mts_order_signal(
        signal, _bound_strategy(), {}, __import__("datetime").datetime.now())

    assert calls == []               # OrderManager never reached
    assert monitor.api.calls == []   # adapter never reached
    blocked = [e for e in events if e[0] == "ORDER_INTENT_BLOCKED"]
    assert blocked and blocked[0][1]["reason"] == "LIVE_ORDER_AUTHORIZATION_FAILED"


def test_e2e_real_chain_registry_injected_into_broker_adapter(monkeypatch):
    """[audit #1] real monitor -> OrderManager -> ShioajiClient chain: the
    registry lands on the actual broker_adapter and the real _gate_or_raise
    enforces it (direct calls rejected, gateway-submitted orders pass)."""
    from pathlib import Path
    from types import SimpleNamespace
    import strategies.futures.monitor as monitor_mod
    from strategies.futures.squeeze_futures.data.shioaji_client import (
        ShioajiClient,
    )

    client = ShioajiClient.__new__(ShioajiClient)
    client._execution_context = _live_ctx()
    placed = []

    def _unchecked(contract, action, quantity, price):
        placed.append({"code": contract.code, "action": action})
        return SimpleNamespace(exchange_order_id=f"BROKER-{action}",
                               seqno="1")

    client.get_contract = lambda s: SimpleNamespace(code=s)
    client._place_order_unchecked = _unchecked

    monitor, events = _monitor(_live_ctx())
    monitor.api = client
    monitor.order_mgr = OrderManager(mode="live", broker_adapter=client,
                                     execution_context=_live_ctx())
    monkeypatch.setattr(monitor_mod, "is_taifex_futures_market_open",
                        lambda: True)
    monkeypatch.setattr(monitor_mod, "_mts_position_state_path",
                        lambda: Path("/tmp/test_s0_no_state.json"))
    signal = SimpleNamespace(action="BUY_NEAR_SELL_FAR", reason="ENTRY")
    strat = _bound_strategy()
    strat._has_position = False

    monitor._submit_mts_order_signal(
        signal, strat, {}, __import__("datetime").datetime.now())

    # the REAL broker_adapter object received the registry
    assert client._gateway_registry is not None
    assert client._gateway_registry is monitor.order_mgr.broker_adapter._gateway_registry
    # the real chain placed both entry legs through the real _gate_or_raise
    assert len(placed) == 2

    # direct adapter call (no gateway authorization) -> REAL gate rejects
    with pytest.raises(Exception) as exc:
        client.place_order_object(_order("ORD-DIRECT", "MTS_ENTRY"))
    assert "ADAPTER_GATEWAY_AUTHORIZATION_MISSING" in str(exc.value)


def test_e2e_oco_blocked_in_exit_only():
    """[audit #5] OCO reconcile paths are blocked before Order construction
    in EXIT_ONLY — zero OrderManager submit, zero adapter call."""
    from types import SimpleNamespace
    from strategies.futures.monitor import FuturesMonitor

    monitor, events = _monitor(_exit_only_ctx())
    submitted = []
    monitor.paper_fill_sim = SimpleNamespace(register=lambda o: None)

    def _recording_submit(order):
        submitted.append(order.order_id)
        return True

    monitor.order_mgr.submit = _recording_submit

    # both OCO reconcile entry points must return before any Order/submit
    monitor._reconcile_paper_oco_orders_from_state()
    monitor._reconcile_paper_oco_orders(_bound_strategy())

    assert submitted == []
    assert monitor.api.calls == []
    blocked = [e for e in events if e[0] == "ORDER_INTENT_BLOCKED"]
    assert blocked and all(
        b[1]["reason"] == "GATEWAY_OCO_DISABLED" for b in blocked)


def test_exit_intent_failed_leg_never_submitted(tmp_path):
    """[audit #6] a failed gateway submit (raise) must never leave the
    exit-intent leg SUBMITTED — it lands in durable UNKNOWN instead."""
    from core.exit_intent import IntentLog
    from core.order_intent_gateway import GatewaySubmitError

    log = IntentLog(log_dir=str(tmp_path))
    iid = log.create("mts-t1", "COMBINED_EXIT")

    def _fail(cid, leg):
        raise GatewaySubmitError("SUBMIT_REJECTED")

    with pytest.raises(GatewaySubmitError):
        log.submit_leg(iid, "NEAR", order_mgr=None, submit_fn=_fail)

    st = log.get(iid)["legs"]["NEAR"]["status"]
    assert st == "UNKNOWN"
    assert st != "SUBMITTED"
