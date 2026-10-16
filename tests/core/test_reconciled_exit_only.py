"""Hard-gate contract for a broker-reconciled, exit-only MTS spread."""

import pytest

from core.mode_transition import (
    ExecutionContext,
    LiveOrderBlocked,
    ModeTransitionState,
)
from core.order_management.order import Order, OrderSide, OrderType


CAPABILITY = {
    "reconciliation_id": "recon-20260811-manual-085503",
    "allowed_orders": (
        {"symbol": "TMFH6", "side": "buy", "remaining_qty": 1},
        {"symbol": "TMFI6", "side": "sell", "remaining_qty": 1},
    ),
}


def _ctx():
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.RECONCILED_EXIT_ONLY.value,
        live_order_allowed=False,
        exit_only_capability=CAPABILITY,
    )


def _order(symbol="TMFH6", side=OrderSide.BUY, quantity=1,
           strategy="MTS_EXIT", reconciliation_id="recon-20260811-manual-085503"):
    order = Order(
        symbol=symbol,
        side=side,
        order_type=OrderType.MKP,
        quantity=quantity,
        strategy=strategy,
    )
    # The production constructor will own this field after the GREEN change.
    # Assigning it here keeps the RED suite focused on authorization behavior.
    order.reconciliation_id = reconciliation_id
    return order


def test_exit_only_allows_exact_bound_close_order():
    _ctx().assert_order_allowed(_order(), method="place_order")


@pytest.mark.parametrize("order", [
    _order(strategy="MTS_ENTRY"),
    _order(symbol="TMFH6", side=OrderSide.SELL),
    _order(symbol="TMFI6", side=OrderSide.BUY),
    _order(symbol="TMFZ6"),
    _order(quantity=2),
    _order(reconciliation_id=None),
], ids=["entry", "wrong-near-side", "wrong-far-side", "wrong-symbol", "too-many", "no-reconciliation-id"])
def test_exit_only_default_denies_any_nonbound_order(order):
    with pytest.raises(LiveOrderBlocked):
        _ctx().assert_order_allowed(order, method="place_order")


def test_exit_only_blocks_generic_update_and_cancel():
    ctx = _ctx()
    with pytest.raises(LiveOrderBlocked):
        ctx.assert_order_allowed(None, method="update_order")
    with pytest.raises(LiveOrderBlocked):
        ctx.assert_order_allowed(None, method="cancel_order")


def test_exit_only_capability_is_persisted_without_granting_live_ready():
    ctx = _ctx()
    persisted = ctx.to_dict()
    assert persisted["effective_mode"] == "reconciled_exit_only"
    assert persisted["live_order_allowed"] is False
    assert persisted["exit_only_capability"] == CAPABILITY
    assert ctx.is_live_ready() is False
