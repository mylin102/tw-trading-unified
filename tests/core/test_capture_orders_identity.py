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
        # The current futures refresh contract calls update_status(account)
        # followed by the SDK's no-argument list_trades() stream.
        return [SimpleNamespace(
            status=SimpleNamespace(status="PendingSubmit", status_code="P",
                                   order_quantity=1, deal_quantity=0,
                                   cancel_quantity=0, deals=[]),
            order=SimpleNamespace(id="2353c7b0", ordno="2353c7b0",
                                  seqno="756569", quantity=1),
            contract=SimpleNamespace(code="TMFI6"))]

    def margin(self, *, account):
        return SimpleNamespace(available_margin=500000.0)

    def update_status(self, *, account=None, trade=None, timeout=None):
        return None

    def snapshots(self, contracts):
        return []

    def order_deal_records(self, account=None, **kwargs):
        return []


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
    assert oo[0]["status"] == "PendingSubmit"
    # Non-terminal open_orders is intentionally a minimal projection;
    # identity-bearing refreshed Trade evidence is kept separately.
    tr = (payload.get("broker_trades") or [])[0]
    assert tr["broker_order_id"] == "2353c7b0"
    assert tr["ordno"] == "2353c7b0"
    assert tr["seqno"] == "756569"
    assert tr["code"] == "TMFI6"
    assert payload.get("available_margin") == 500000.0
