"""C1 fills-callback bridge acceptance tests (RED -> GREEN).

Locks:
- exactly-once: duplicate broker deal callbacks apply the fill once
- malformed/ambiguous callbacks fail closed (no state change, no ledger)
- fills ledger path unifies on runtime_paths (TRADING_RUNTIME_DIR), env override
- options monitor must not re-register the order callback (sole owner: central
  dispatcher registered by main.py)
"""
import os
from types import SimpleNamespace

import pytest

from core.order_management.order import Order, OrderSide, OrderStatus, OrderType
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
    assert mon._fills_bridge_mark_seen("ORD-1", "deal-1", "SEQ-1") is True
    assert mon._fills_bridge_mark_seen("ORD-1", "deal-1", "SEQ-1") is False
    assert mon._fills_bridge_mark_seen("ORD-1", "deal-2", "SEQ-1") is True


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


# ── malformed / ambiguous fail-closed ──

def test_malformed_deal_zero_quantity_fails_closed():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    mon = _monitor_with(manager)
    applied = []
    manager.apply_deal_fill = lambda *a, **k: (applied.append(k), None)

    mon.on_order_event(None, _deal_data(quantity=0))

    assert applied == []
    assert order.status is OrderStatus.SUBMITTED
    assert order.filled_quantity == 0


def test_malformed_deal_zero_price_fails_closed():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    mon = _monitor_with(manager)
    applied = []
    manager.apply_deal_fill = lambda *a, **k: (applied.append(k), None)

    mon.on_order_event(None, _deal_data(price=0))

    assert applied == []
    assert order.status is OrderStatus.SUBMITTED


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
