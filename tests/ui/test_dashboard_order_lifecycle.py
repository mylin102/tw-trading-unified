"""Dashboard order-lifecycle display tests.

[dashboard lifecycle slice] per-leg TERMINAL-state rows: only
FILLED / CANCELLED / REJECTED / TIMEOUT / EXPIRED orders are shown with
their terminal time, filled quantity and fill price (missing => N/A);
quote-observation records (EXIT_ONLY_BBO_OBSERVED) are observation-only
and are never displayed as a trigger; paper/live/exit-only rows are
never mixed (the caller passes the already-isolated presentation set).
"""
import pytest

from ui.dashboard import mts_order_lifecycle_rows


def _order(**over):
    base = {
        "order_id": "ORD-1",
        "symbol": "TMFH6",
        "side": "buy",
        "status": "filled",
        "filled_at": "2026-08-12T09:18:55.159",
        "filled_quantity": 1,
        "avg_fill_price": 45285.0,
        "strategy": "MTS_RELEASE",
    }
    base.update(over)
    return base


def test_lifecycle_rows_terminal_states():
    """FILLED / CANCELLED / REJECTED show their terminal time, filled
    qty and price; the status is uppercased."""
    rows = mts_order_lifecycle_rows([
        _order(),
        _order(order_id="ORD-2", symbol="TMFI6", status="rejected",
               rejected_at="2026-08-12T09:18:55.300",
               reject_reason="LIVE_ORDER_AUTHORIZATION_FAILED"),
        _order(order_id="ORD-3", status="cancelled",
               cancelled_at="2026-08-12T10:00:00.000",
               cancel_reason="manual"),
    ])
    assert len(rows) == 3
    by_id = {r["order_id"]: r for r in rows}
    assert by_id["ORD-1"]["status"] == "FILLED"
    assert by_id["ORD-1"]["terminal_at"] == "2026-08-12T09:18:55.159"
    assert by_id["ORD-1"]["filled_qty"] == 1
    assert by_id["ORD-1"]["price"] == 45285.0
    assert by_id["ORD-2"]["status"] == "REJECTED"
    assert by_id["ORD-2"]["reason"] == "LIVE_ORDER_AUTHORIZATION_FAILED"
    assert by_id["ORD-3"]["status"] == "CANCELLED"


def test_lifecycle_rows_timeout_included():
    """TIMEOUT is a terminal state and is displayed."""
    rows = mts_order_lifecycle_rows([
        _order(order_id="ORD-4", status="timeout",
               filled_at=None, rejected_at=None, cancelled_at=None,
               expired_at="2026-08-12T10:05:00.000")])
    assert len(rows) == 1
    assert rows[0]["status"] == "TIMEOUT"
    assert rows[0]["terminal_at"] == "2026-08-12T10:05:00.000"


def test_lifecycle_rows_missing_data_na():
    """Missing terminal fields render as N/A, never as a bare blank."""
    rows = mts_order_lifecycle_rows([
        _order(order_id="ORD-5",
               filled_at=None, rejected_at=None, cancelled_at=None,
               expired_at=None)])
    assert len(rows) == 1
    # no filled_at/rejected_at/cancelled_at/expired_at in this record
    assert rows[0]["terminal_at"] == "N/A"
    r2 = mts_order_lifecycle_rows([
        _order(order_id="ORD-6", filled_at=None, rejected_at=None,
               cancelled_at=None, expired_at=None,
               filled_quantity=None, avg_fill_price=None, price=None)])
    assert r2[0]["filled_qty"] == "N/A"
    assert r2[0]["price"] == "N/A"


def test_lifecycle_rows_pending_and_observation_excluded():
    """Pending orders and quote-observation records are NOT displayed as
    triggers — only terminal states produce rows."""
    rows = mts_order_lifecycle_rows([
        _order(order_id="ORD-7", status="submitted"),
        _order(order_id="ORD-8", status="pending_submit"),
        {"event": "EXIT_ONLY_BBO_OBSERVED", "bbo_hash": "abc",
         "ts": "2026-08-12T09:18:55.000"},  # observation, NOT a trigger
        {"event": "ORDER_INTENT_BLOCKED", "reason": "QUOTE_INVALID"},
        _order(order_id="ORD-9", status="filled"),
    ])
    assert [r["order_id"] for r in rows] == ["ORD-9"]
    assert all(r["order_id"] != "N/A" or r["status"] for r in rows)
