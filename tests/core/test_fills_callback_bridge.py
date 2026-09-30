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
import json
import os
from collections.abc import Mapping
from pathlib import Path
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


def test_restart_safe_dedupe_seqno_only_receipt():
    """A restored order whose durable fill has exchange_seq but no deal id must
    still dedupe a post-restart seqno-only duplicate callback (no reapply)."""
    manager = OrderManager(mode="paper")
    order = _order(manager)
    # Restored fill: broker identity carried only via exchange_seq.
    order.fills.append(OrderFill(
        order_id=order.order_id, fill_price=46329.0, fill_quantity=1,
        deal_id=None, exchange_fill_id=None, broker_trade_id=None,
        exchange_seq="SEQ-9", fill_time=None,
    ))
    order.filled_quantity = 1
    order.status = OrderStatus.FILLED
    mon = _monitor_with(manager)
    applied = []
    manager.apply_deal_fill = lambda *a, **k: (applied.append(k), None)

    order_state = SimpleNamespace(value="FDEAL")
    # seqno (order identity) matches the order; fill identity carried only via
    # exchange_seq; no deal id present.
    mon.on_order_event(order_state, _deal_data(
        trade_id="", id="", seqno="SEQ-1", ordno="",
        exchange_seq="SEQ-9",
        order=SimpleNamespace(id="", ordno="", seqno="")))

    assert applied == []
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


def test_order_manager_fill_receipt_requires_identity_and_is_idempotent():
    """The canonical manager boundary also enforces receipt invariants."""
    manager = OrderManager(mode="paper")
    order = _order(manager)
    assert manager.apply_deal_fill(
        None, fill_price=46329.0, fill_qty=1, broker_order_id="BRK-1"
    ) is None
    assert order.filled_quantity == 0

    assert manager.apply_deal_fill(
        None, fill_price=46329.0, fill_qty=1, broker_order_id="BRK-1",
        deal_id="deal-1", exchange_seq="SEQ-1"
    ) is order
    assert manager.apply_deal_fill(
        None, fill_price=46329.0, fill_qty=1, broker_order_id="BRK-1",
        deal_id="deal-1", exchange_seq="SEQ-1"
    ) is order
    assert order.filled_quantity == 1
    assert len(order.fills) == 1


# ── ledger path unification ──

def test_fills_ledger_path_resolves_runtime_logs(monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", "/tmp/rt-unified-test")
    monkeypatch.delenv("MTS_FILL_LOG_PATH", raising=False)
    from strategies.plugins.futures.active import tmf_spread
    assert tmf_spread._fills_ledger_path() == str(Path("/tmp/rt-unified-test/logs/mts_trade_fills.jsonl").resolve())


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


# ── live 2026-09-24: a spread fill is quoted NEGATIVE ──

def test_negative_spread_price_deal_is_applied():
    """RED: the real -144 calendar-spread fill was rejected as
    INVALID_PRICE_OR_QUANTITY, so the entry never reached the ledger and the
    position was later reset to FLAT while the broker still held the spread."""
    manager = OrderManager(mode="paper")
    order = _order(manager)
    mon = _monitor_with(manager)
    applied = []
    _orig = manager.apply_deal_fill
    manager.apply_deal_fill = lambda *a, **k: (applied.append(k), _orig(*a, **k))[1]

    mon.on_order_event(SimpleNamespace(value="FDEAL"),
                       _deal_data(price=-144.0, trade_id="deal-neg-1"))

    assert len(applied) == 1, [e for e in mon.events]
    assert applied[0]["fill_price"] == -144.0
    assert applied[0]["fill_qty"] == 1
    assert not [e for e in mon.events if e[0] == "FDEAL_CALLBACK_REJECTED"]


def test_rejected_deal_records_the_offending_values():
    """A rejection must name the values it rejected (the live rejection carried
    only a reason, which is why this was invisible for a full session)."""
    manager = OrderManager(mode="paper")
    order = _order(manager)
    mon = _monitor_with(manager)
    manager.apply_deal_fill = lambda *a, **k: None

    mon.on_order_event(SimpleNamespace(value="FDEAL"), _deal_data(price=None))

    rejected = [e for e in mon.events if e[0] == "FDEAL_CALLBACK_REJECTED"]
    assert len(rejected) == 1, mon.events
    payload = rejected[0][1]
    assert payload["rejection_reason"] == "INVALID_PRICE_OR_QUANTITY"
    assert payload["raw_price"] is None
    assert payload["raw_quantity"] == 1
    assert payload["broker_order_id"] == "BRK-1"


class _LiveFDealPayload(Mapping):
    """Dict-like callback shape used by the live Shioaji dispatcher.

    The object intentionally has neither ``dict`` inheritance nor
    ``__dict__``.  This is the shape that exposed the production bridge bug:
    ``msg.get(...)`` works, while the old serializer collapsed the receipt to
    ``{"value": "..."}`` and therefore observed every canonical field as
    null.
    """

    __slots__ = ("_payload",)

    def __init__(self, **payload):
        self._payload = payload

    def __getitem__(self, key):
        return self._payload[key]

    def __iter__(self):
        return iter(self._payload)

    def __len__(self):
        return len(self._payload)


def test_live_combo_fdeal_replay_lands_two_leg_prices_once(tmp_path, monkeypatch):
    """Replay the 2026-09-30 09:56:10 two-leg FDEAL sequence through the
    production bridge.  The broker combo id is shared by both leg receipts;
    leg identity comes from contract/exchange receipt content.
    """
    from strategies.plugins.futures.active import tmf_spread

    fill_log = tmp_path / "mts_trade_fills.jsonl"
    monkeypatch.setattr(tmf_spread, "_MTS_FILL_LOG", str(fill_log))

    manager = OrderManager(mode="paper")
    order = Order(
        "TMFJ6-TMFK6", OrderSide.BUY, OrderType.MKP, 1,
        order_id="ORD-20260930-000248", strategy="MTS_ENTRY",
        truth_source="broker_combo",
        combo_legs=[
            {"symbol": "TMFJ6", "side": "buy", "quantity": 1},
            {"symbol": "TMFK6", "side": "sell", "quantity": 1},
        ],
        combo_strategy="TIME_SPREAD",
    )
    manager.active_orders[order.order_id] = order
    order.submit("2a97043a", broker_order_id="2a97043a", ordno="2a97043a")

    mon = _monitor_with(manager)
    mon._mts_pending_fills = {
        "mts-auto-095609-997": {
            "combo_order_id": order.order_id,
            "combo": True,
            "combo_legs": list(order.combo_legs),
            "near_filled": False,
            "far_filled": False,
            "side": "LONG",
            "spread_side": "BUY_NEAR_SELL_FAR",
            "near_label": "NEAR",
            "far_label": "FAR",
        }
    }

    def _persist_confirmed_combo(event):
        pending = mon._mts_pending_fills["mts-auto-095609-997"]
        tmf_spread._append_fill(
            "TMF", "TMFJ6", "NEAR", "LONG", 1,
            pending["near_fill_price"], "ENTRY", "mts-auto-095609-997")
        tmf_spread._append_fill(
            "TMF", "TMFK6", "FAR", "SHORT", 1,
            pending["far_fill_price"], "ENTRY", "mts-auto-095609-997")

    manager.register_callback("on_fill", _persist_confirmed_combo)

    callbacks = [
        _LiveFDealPayload(
            trade_id="2a97043a", seqno="787254", ordno=None,
            exchange_seq=None, action="Buy",
            code="TMFJ6", price=48605.0, quantity=1, combo=True,
            ts=1790733370.414,
        ),
        _LiveFDealPayload(
            trade_id="2a97043a", seqno="787254", ordno=None,
            exchange_seq=None, action="Sell",
            code="TMFK6", price=48754.0, quantity=1, combo=True,
            ts=1790733370.450,
        ),
    ]
    state = SimpleNamespace(name="FuturesDeal", value="FDEAL")

    for callback in callbacks:
        mon.on_order_event(state, callback)
    for callback in callbacks:  # broker replay must be idempotent
        mon.on_order_event(state, callback)

    rows = [json.loads(line) for line in fill_log.read_text().splitlines()]
    assert [(row["leg"], row["price"]) for row in rows] == [
        ("NEAR", 48605.0),
        ("FAR", 48754.0),
    ]
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 1
    assert len(order.fills) == 1
    assert order.fills[0].deal_id.startswith("combo-callback:")
