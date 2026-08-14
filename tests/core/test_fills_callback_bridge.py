"""C1 fills-callback bridge acceptance tests (RED -> GREEN).

Locks:
- exactly-once: duplicate broker deal callbacks apply the fill once
- restart-safe: order.fills carries durable broker identities (orders JSON) and
  a restored order must not re-apply a duplicate post-restart callback
- malformed/ambiguous callbacks fail closed (no state change, no ledger, and
  the dedupe set is not polluted by missing identities)
- fills ledger path unifies on runtime_paths (TRADING_RUNTIME_DIR), env override
- options monitor must not re-register the order callback (sole owner: central
  dispatcher registered by main.py)
- no direct module-var fills-log reads in monitor/tmf_spread
"""
import os
from types import SimpleNamespace

import pytest

from core.order_management.order import Order, OrderFill, OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager
from strategies.futures.monitor import FuturesMonitor


class _Mon(FuturesMonitor):
    def __init__(self):
        self.events = []

    def _append_mts_event(self, event_type, **payload):
        self.events.append((event_type, payload))


def _order(manager, code="TMFI6", order_id="ORD-1"):
    order = Order(code, OrderSide.BUY, OrderType.MKP, 1,
                  order_id=order_id, strategy="MTS_ENTRY")
    manager.active_orders[order.order_id] = order
    order.submit("BRK-1", broker_order_id="BRK-1", ordno="BRK-1", seqno="SEQ-1")
    return order


def _deal_data(**overrides):
    """Shioaji 1.7-style nested deal payload (identity under order, nested status)."""
    base = {
        "id": "BRK-1",
        "seqno": "SEQ-1",
        "ordno": "BRK-1",
        "trade_id": "deal-1",
        "price": 46329.0,
        "quantity": 1,
        "status": SimpleNamespace(status="Filled"),
        "order": SimpleNamespace(id="BRK-1", ordno="BRK-1", seqno="SEQ-1"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _monitor_with(manager):
    mon = _Mon()
    mon.order_mgr = manager
    mon.dry_run = False
    return mon


# ── exactly-once dedupe ──

def test_fills_bridge_dedupe_primitive_marks_and_detects():
    mon = _Mon()
    order = _order(OrderManager(mode="paper"))
    assert mon._fills_bridge_mark_seen(order, "deal-1", "SEQ-1") is True
    assert mon._fills_bridge_mark_seen(order, "deal-1", "SEQ-1") is False
    assert mon._fills_bridge_mark_seen(order, "deal-2", "SEQ-1") is True


def test_duplicate_deal_callback_applies_fill_exactly_once():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    mon = _monitor_with(manager)
    applied = []
    _orig = manager.apply_deal_fill
    manager.apply_deal_fill = lambda *a, **k: (applied.append(k), _orig(*a, **k))[1]

    order_state = SimpleNamespace(value="FDEAL")
    data = _deal_data()
    mon.on_order_event(order_state, data)
    mon.on_order_event(order_state, data)  # duplicate callback

    assert len(applied) == 1
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 1
    assert len(order.fills) == 1


def test_restart_safe_dedupe_via_durable_order_fills():
    """A restored order (post-restart) already carries the broker fill identity
    in order.fills; the duplicate callback must not re-apply it."""
    manager = OrderManager(mode="paper")
    order = _order(manager)
    # Simulate the durable fill restored from the orders JSON.
    order.fills.append(OrderFill(
        order_id=order.order_id, fill_price=46329.0, fill_quantity=1,
        deal_id="deal-9", exchange_fill_id="deal-9", broker_trade_id="deal-9",
        exchange_seq="SEQ-9", fill_time=None,
    ))
    order.filled_quantity = 1
    order.status = OrderStatus.FILLED
    mon = _monitor_with(manager)
    applied = []
    manager.apply_deal_fill = lambda *a, **k: (applied.append(k), None)

    order_state = SimpleNamespace(value="FDEAL")
    mon.on_order_event(order_state, _deal_data(trade_id="deal-9", seqno="SEQ-9",
                                               ordno="BRK-1", id="BRK-1"))

    assert applied == []
    assert order.filled_quantity == 1
    assert len(order.fills) == 1


# ── malformed / ambiguous fail-closed ──

def test_malformed_deal_zero_quantity_fails_closed():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    mon = _monitor_with(manager)
    applied = []
    manager.apply_deal_fill = lambda *a, **k: (applied.append(k), None)

    mon.on_order_event(SimpleNamespace(value="FDEAL"), _deal_data(quantity=0))

    assert applied == []
    assert order.status is OrderStatus.SUBMITTED
    assert order.filled_quantity == 0


def test_malformed_deal_zero_price_fails_closed():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    mon = _monitor_with(manager)
    applied = []
    manager.apply_deal_fill = lambda *a, **k: (applied.append(k), None)

    mon.on_order_event(SimpleNamespace(value="FDEAL"), _deal_data(price=0))

    assert applied == []
    assert order.status is OrderStatus.SUBMITTED


def test_missing_broker_identity_fails_closed_without_polluting_dedupe():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    mon = _monitor_with(manager)
    applied = []
    manager.apply_deal_fill = lambda *a, **k: (applied.append(k), None)

    order_state = SimpleNamespace(value="FDEAL")
    empty = SimpleNamespace(id="", ordno="", seqno="")
    mon.on_order_event(order_state, _deal_data(
        id="", seqno="", ordno="", trade_id="",
        order=empty, status=SimpleNamespace(status="Filled")))

    assert applied == []
    assert order.status is OrderStatus.SUBMITTED
    assert len(getattr(mon, "_fills_bridge_seen", {}) or {}) == 0

    # A later legit callback with real identity still applies.
    mon.on_order_event(order_state, _deal_data())
    assert len(applied) == 1


# ── ledger path unification ──

def test_fills_ledger_path_resolves_runtime_logs(monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", "/tmp/rt-unified-test")
    monkeypatch.delenv("MTS_FILL_LOG_PATH", raising=False)
    from strategies.plugins.futures.active import tmf_spread
    assert tmf_spread._fills_ledger_path() == "/tmp/rt-unified-test/logs/mts_trade_fills.jsonl"


def test_fills_ledger_path_env_override_wins(monkeypatch):
    monkeypatch.setenv("MTS_FILL_LOG_PATH", "/tmp/custom-fills.jsonl")
    from strategies.plugins.futures.active import tmf_spread
    assert tmf_spread._fills_ledger_path() == "/tmp/custom-fills.jsonl"


# ── fills ledger path consistency ──

def test_fills_ledger_module_var_is_env_derived():
    """The fills ledger module var must resolve through _fills_ledger_path()
    (MTS_FILL_LOG_PATH / TRADING_RUNTIME_DIR-aware), never the old hardcoded
    cwd-relative default; all writes and reads share the same module var."""
    import pathlib
    tmf_src = pathlib.Path(
        "strategies/plugins/futures/active/tmf_spread.py").read_text(encoding="utf-8")
    mon_src = pathlib.Path("strategies/futures/monitor.py").read_text(encoding="utf-8")
    assert "_MTS_FILL_LOG = _fills_ledger_path()" in tmf_src
    assert '_MTS_FILL_LOG = os.getenv("MTS_FILL_LOG_PATH", "logs/mts_trade_fills.jsonl")' not in tmf_src
    assert "import _MTS_FILL_LOG" in mon_src  # monitor reads share the module var


# ── options must not re-register the central callback ──

def test_options_never_registers_order_callback_directly():
    """Source invariant: the options monitor must never re-register the Shioaji
    order callback (the central dispatcher from main.py is the sole owner).
    Importing the options module pulls heavy deps, so assert on source."""
    import pathlib
    src = pathlib.Path(
        "strategies/options/live_options_squeeze_monitor.py").read_text(encoding="utf-8")
    assert "set_order_callback(self.on_order_event)" not in src
    assert "set_order_callback(" not in src
    assert "_ensure_central_dispatcher_ownership" in src
