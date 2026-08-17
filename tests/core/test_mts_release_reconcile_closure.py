"""P0-B: fill/lifecycle closure — FDEAL/FORDER nested Shioaji status and
order_deal_records/list_trades reconciliation must update the terminal order
AND the fills ledger / MTS lifecycle exactly once, including MTS_RELEASE.

Bounded contract (postmortem plan, 2026-08-17):
- FORDER rows (nested order status) normalize to terminal order receipts and
  reconcile the local order exactly once (no callback-only assumption)
- a broker-confirmed release fill advances the MTS lifecycle to SINGLE_LEG
  via sync_release exactly once (leg + release price + remaining-leg price)
- restart/backfill: repeated refresh cycles with the same receipt are no-ops
- duplicate identity: same deal twice -> fills_added once, lifecycle once
- query failure / capture failure -> fail-closed: no order mutation, no
  lifecycle advance
- non-release fills never touch the release lifecycle
"""
from types import SimpleNamespace

from core.order_management.order import OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager
from strategies.futures.monitor import FuturesMonitor


# ── fixtures ────────────────────────────────────────────────────────────────

def _monitor():
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.contract = SimpleNamespace(code="TMFH6")
    mon.far_contract = SimpleNamespace(code="TMFI6")
    mon.ticker = "TMF"
    mon.market_data = {}
    mon._current_bar = {}
    mon._far_current_bar = {}
    mon._save_orders_file_wrapper = lambda: None
    mon._emit_fill_rejected = lambda *a, **kw: None
    mon._registry = {"tmf_spread": None}
    mon._mts_strategy = None
    return mon


def _release_strategy():
    calls = []

    class _S:
        _near_entry = 45879.0
        _far_entry = 46033.0
        _trade_id = "mts-t-1"

        def sync_release(self, **kw):
            calls.append(kw)

    return _S(), calls


def _snap(broker_trades, capture="OK"):
    return {
        "source": "live_broker",
        "fetch_status": {"capture": capture},
        "positions": [],
        "open_orders": [],
        "broker_trades": list(broker_trades),
    }


def _deal_row(order_id="BRK-REL", code="TMFI6", price=46016.0, qty=1,
              deal_id="D-1"):
    """Shape produced by _normalize_order_deal_records (FDEAL)."""
    return {
        "id": order_id, "broker_order_id": order_id, "ordno": "ORD-9",
        "seqno": "SEQ-9", "code": code, "delivery_month": "202609",
        "status": "Filled", "price": price, "quantity": qty,
        "filled_quantity": qty, "trade_id": order_id, "ts": 1786690800.0,
        "deals": [{"trade_id": deal_id, "broker_trade_id": deal_id,
                   "exchange_fill_id": deal_id, "exchange_seq": "EX-1",
                   "price": price, "quantity": qty, "ordno": "ORD-9"}],
    }


def _forder_row(order_id="BRK-REL", code="TMF", delivery_month="202609",
                price=46016.0, deal_id="D-1"):
    """Raw Shioaji order_deal_records (FuturesOrder, nested status)."""
    state = SimpleNamespace(name="FuturesOrder", value="FORDER")
    payload = {
        "order": {"id": order_id, "ordno": "ORD-9", "seqno": "SEQ-9"},
        "status": SimpleNamespace(status=SimpleNamespace(name="Filled")),
        "contract": {"code": code, "delivery_month": delivery_month},
        "deals": [{"deal_id": deal_id, "price": price, "quantity": 1}],
        "ts": 1786690800.0,
    }
    return (state, payload)


def _release_order(manager, symbol="TMFI6", strategy="MTS_RELEASE"):
    order = manager.create_order(symbol=symbol, side=OrderSide.BUY,
                                 order_type=OrderType.MARKET, quantity=1,
                                 strategy=strategy)
    order.submit("BRK-REL", broker_order_id="BRK-REL",
                 seqno="SEQ-9", ordno="ORD-9")
    return order


# ── FORDER normalization ────────────────────────────────────────────────────

def test_forder_nested_status_normalizes_to_order_receipt():
    rows = FuturesMonitor._normalize_order_state_records([_forder_row()])
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "BRK-REL"
    assert row["broker_order_id"] == "BRK-REL"
    assert row["ordno"] == "ORD-9" and row["seqno"] == "SEQ-9"
    assert row["status"] == "Filled"          # nested status, not wrapper
    assert row["code"] == "TMFI6"             # delivery-month code rebuilt
    assert len(row["deals"]) == 1
    assert row["deals"][0]["price"] == 46016.0
    assert row["deals"][0]["quantity"] == 1
    assert row["ts"] == 1786690800.0


def test_forder_row_reconciles_terminal_order_exactly_once():
    manager = OrderManager(mode="live")
    order = _release_order(manager)
    rows = FuturesMonitor._normalize_order_state_records([_forder_row()])
    first = manager.reconcile_broker_state(filled_trades=rows,
                                           source="order_state_records")
    second = manager.reconcile_broker_state(filled_trades=rows,
                                            source="order_state_records")
    assert len(first["reconciled"]) == 1
    assert first["reconciled"][0]["fills_added"] == 1
    assert second["reconciled"] == []
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 1
    assert order.avg_fill_price == 46016.0


# ── MTS release lifecycle closure ───────────────────────────────────────────

def test_release_fill_reconcile_closes_lifecycle_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    mon = _monitor()
    manager = OrderManager(mode="live")
    mon.order_mgr = manager
    order = _release_order(manager)
    strat, calls = _release_strategy()
    mon._mts_strategy = strat
    saved = []
    mon._save_orders_file_wrapper = lambda: saved.append(1)
    snap = _snap([_deal_row()])

    changed = mon._reconcile_local_orders_from_snapshot(snap)

    assert order.status is OrderStatus.FILLED
    assert changed == 1
    assert saved == [1]                       # durable orders-file save
    assert len(calls) == 1
    assert calls[0]["leg"] == "far"
    assert calls[0]["release_price"] == 46016.0
    # remaining-leg price falls back to the strategy entry when no quote
    assert calls[0]["price"] == strat._far_entry
    assert calls[0]["order_id"] == order.order_id
    assert calls[0]["event_time"] is not None

    # exactly once: same receipt on the next refresh is a no-op
    assert mon._reconcile_local_orders_from_snapshot(snap) == 0
    assert len(calls) == 1


def test_release_fill_near_leg_uses_entry_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    mon = _monitor()
    manager = OrderManager(mode="live")
    mon.order_mgr = manager
    _release_order(manager, symbol="TMFH6")
    strat, calls = _release_strategy()
    mon._mts_strategy = strat

    mon._reconcile_local_orders_from_snapshot(
        _snap([_deal_row(code="TMFH6", price=45850.0)]))

    assert len(calls) == 1
    assert calls[0]["leg"] == "near"
    assert calls[0]["release_price"] == 45850.0
    assert calls[0]["price"] == strat._far_entry   # remaining = far entry


def test_forder_release_reconcile_closes_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    mon = _monitor()
    manager = OrderManager(mode="live")
    mon.order_mgr = manager
    order = _release_order(manager)
    strat, calls = _release_strategy()
    mon._mts_strategy = strat

    rows = FuturesMonitor._normalize_order_state_records([_forder_row()])
    mon._reconcile_local_orders_from_snapshot(_snap(rows))

    assert order.status is OrderStatus.FILLED
    assert len(calls) == 1
    assert calls[0]["leg"] == "far"
    assert calls[0]["release_price"] == 46016.0


def test_duplicate_deal_identity_no_duplicate_fill(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    mon = _monitor()
    manager = OrderManager(mode="live")
    mon.order_mgr = manager
    _release_order(manager)
    strat, calls = _release_strategy()
    mon._mts_strategy = strat

    # the same deal id appears twice in one snapshot
    snap = _snap([_deal_row(), _deal_row(deal_id="D-1")])
    changed = mon._reconcile_local_orders_from_snapshot(snap)

    assert changed == 1
    assert len(calls) == 1


def test_query_failure_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    mon = _monitor()
    manager = OrderManager(mode="live")
    mon.order_mgr = manager
    order = _release_order(manager)
    strat, calls = _release_strategy()
    mon._mts_strategy = strat

    assert mon._reconcile_local_orders_from_snapshot(
        _snap([_deal_row()], capture="FAIL")) == 0
    assert order.status is not OrderStatus.FILLED
    assert calls == []


def test_non_release_order_fill_does_not_advance_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    mon = _monitor()
    manager = OrderManager(mode="live")
    mon.order_mgr = manager
    order = _release_order(manager, strategy="MTS_ENTRY")
    strat, calls = _release_strategy()
    mon._mts_strategy = strat

    mon._reconcile_local_orders_from_snapshot(_snap([_deal_row()]))

    assert order.status is OrderStatus.FILLED   # order closed correctly
    assert calls == []                           # but NOT a release closure
