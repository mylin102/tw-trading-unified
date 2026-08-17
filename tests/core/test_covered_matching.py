"""RED tests: covered-matching — broker empty positions + open_orders that
are fully explained by local FILLED entries must NOT keep authority
unresolved (stale PendingSubmit session cache must not block FLAT).

Fail-closed: any open_order without a matching local FILLED order
(identity + direction + qty, one-to-one) keeps the unresolved verdict.
"""
import pytest

from core.broker_evidence import (
    open_orders_fully_covered_by_filled,
)


def _oo(**kw):
    """Canonical open_order row (normalized snapshot shape)."""
    base = {
        "broker_order_id": "B-1", "ordno": "B-1", "seqno": "S-1",
        "code": "TMFH6", "status": "PendingSubmit",
        "direction": "sell", "quantity": 1,
    }
    base.update(kw)
    return base


def _filled(**kw):
    """Local FILLED order evidence row (Order.to_dict shape)."""
    base = {
        "broker_order_id": "B-1", "ordno": "B-1", "seqno": "S-1",
        "symbol": "TMFH6", "side": "sell", "quantity": 1,
        "status": "filled",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_two_entries_covered_by_two_filled_orders():
    oo = [
        _oo(broker_order_id="B-1", code="TMFH6", direction="sell", quantity=1),
        _oo(broker_order_id="B-2", code="TMFI6", direction="buy", quantity=1),
    ]
    filled = [
        _filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1),
        _filled(broker_order_id="B-2", symbol="TMFI6", side="buy", quantity=1),
    ]
    assert open_orders_fully_covered_by_filled(oo, filled) is True


def test_duplicated_capture_rows_still_covered():
    # same order captured twice (dedupe at identity level, one-to-one)
    oo = [
        _oo(broker_order_id="B-1", code="TMFH6", direction="sell", quantity=1),
        _oo(broker_order_id="B-1", code="TMFH6", direction="sell", quantity=1),
    ]
    filled = [_filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1)]
    assert open_orders_fully_covered_by_filled(oo, filled) is True


def test_covered_with_duplicate_filled_rows():
    oo = [_oo(broker_order_id="B-1", code="TMFH6", direction="sell", quantity=1)]
    filled = [
        _filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1),
        _filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1),
    ]
    assert open_orders_fully_covered_by_filled(oo, filled) is True


def test_nested_identity_fallback():
    # identity resolves from nested order.id when top-level absent
    oo = [_oo(broker_order_id=None, ordno=None, seqno=None)]
    filled = [_filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1)]
    assert open_orders_fully_covered_by_filled(oo, filled) is True


# ---------------------------------------------------------------------------
# fail-closed: never covered
# ---------------------------------------------------------------------------

def test_pending_without_any_filled_evidence_is_not_covered():
    oo = [_oo(broker_order_id="B-1")]
    assert open_orders_fully_covered_by_filled(oo, []) is False


def test_direction_mismatch_is_not_covered():
    # local filled says SELL; pending row says BUY (reverse/exit order)
    oo = [_oo(broker_order_id="B-1", code="TMFH6", direction="buy", quantity=1)]
    filled = [_filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1)]
    assert open_orders_fully_covered_by_filled(oo, filled) is False


def test_quantity_mismatch_is_not_covered():
    oo = [_oo(broker_order_id="B-1", code="TMFH6", direction="sell", quantity=2)]
    filled = [_filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1)]
    assert open_orders_fully_covered_by_filled(oo, filled) is False


def test_one_covered_one_real_pending_exit_not_covered():
    # one covered entry + one genuinely pending exit -> NOT flat
    oo = [
        _oo(broker_order_id="B-1", code="TMFH6", direction="sell", quantity=1),
        _oo(broker_order_id="B-9", code="TMFH6", direction="buy", quantity=1),
    ]
    filled = [_filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1)]
    assert open_orders_fully_covered_by_filled(oo, filled) is False


def test_missing_identity_is_not_covered():
    oo = [{"code": "TMFH6", "status": "PendingSubmit", "direction": "sell",
           "quantity": 1}]  # no broker identity anywhere
    filled = [_filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1)]
    assert open_orders_fully_covered_by_filled(oo, filled) is False


def test_partial_filled_local_is_not_covered():
    # local order not terminal FILLED -> cannot explain a broker pending
    oo = [_oo(broker_order_id="B-1", code="TMFH6", direction="sell", quantity=1)]
    filled = [_filled(broker_order_id="B-1", symbol="TMFH6", side="sell", quantity=1,
                      status="partial_filled")]
    assert open_orders_fully_covered_by_filled(oo, filled) is False


def test_symbol_mismatch_is_not_covered():
    oo = [_oo(broker_order_id="B-1", code="TMFH6", direction="sell", quantity=1)]
    filled = [_filled(broker_order_id="B-1", symbol="TMFI6", side="sell", quantity=1)]
    assert open_orders_fully_covered_by_filled(oo, filled) is False


def test_empty_open_orders_is_trivially_covered():
    assert open_orders_fully_covered_by_filled([], []) is True
