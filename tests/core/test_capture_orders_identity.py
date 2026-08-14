"""Step 2 RED: capture open_orders must carry broker identity (nested order.id)
and the futures scope (keyword account call).  Shioaji 1.7 trades omit the
top-level id/code; the identity lives under trade.order.id and the code under
trade.contract.code.  The no-arg list_trades() returns the wrong scope (the
phantom pending rows).  Keyword-only fake api locks the contract.
"""
from types import SimpleNamespace

import pytest

from strategies.futures.monitor import FuturesMonitor


class _FakeApiOrders:
    """Fake api: keyword scoping + shioaji 1.7 nested trade identity."""

    @property
    def futopt_account(self):
        return SimpleNamespace(account_no="F-1")

    @property
    def stock_account(self):
        return None

    def list_positions(self, *, account):
        return []

    def list_trades(self, account=None):
        if account is None:
            # wrong-scope no-arg call: junk rows without identity
            return [SimpleNamespace(status=SimpleNamespace(status="PendingSubmit"))]
        return [SimpleNamespace(
            status=SimpleNamespace(status="PendingSubmit"),
            order=SimpleNamespace(id="2353c7b0", ordno="2353c7b0",
                                  seqno="756569"),
            contract=SimpleNamespace(code="TMFI6"),
            quantity=1)]

    def margin(self, *, account):
        return SimpleNamespace(available_margin=500000.0)


def _monitor(api):
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.api = api
    mon.live_trading = True
    mon.dry_run = False
    mon._append_mts_event = lambda *a, **k: None
    return mon


def test_capture_open_orders_carry_identity_and_futures_scope():
    mon = _monitor(_FakeApiOrders())
    payload = mon._capture_post_startup_snapshot()
    oo = payload.get("open_orders") or []
    assert len(oo) == 1
    assert oo[0]["broker_order_id"] == "2353c7b0"
    assert oo[0]["ordno"] == "2353c7b0"
    assert oo[0]["seqno"] == "756569"
    assert oo[0]["code"] == "TMFI6"
    assert payload.get("available_margin") == 500000.0
