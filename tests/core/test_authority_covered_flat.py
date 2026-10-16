"""RED: live authority resolves stale PendingSubmit session cache to FLAT
when every open order is covered one-to-one by a local FILLED entry.

Two paths under test in FuturesMonitor._refresh_live_broker_authority:
- covered (empty positions + open_orders fully explained by local FILLED)
  -> MtsAuthority.FLAT, strategy._broker_truth_flat True, flat_proven True
- not covered (any open order without a matching local FILLED)
  -> unresolved: authority None, flat_proven False, never flat
"""
from types import SimpleNamespace

from strategies.futures.monitor import FuturesMonitor
from strategies.futures.mts_ledger_authority import MtsAuthority


def _monitor(snapshot, completed_orders):
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon._execution_context = SimpleNamespace(requested_mode="live")
    mon._live_broker_authority_at = 0.0
    mon._live_broker_authority = None
    mon._live_broker_flat_proven = False
    mon._broker_authority_degraded = False
    mon._broker_position_observed = False
    mon.contract = SimpleNamespace(code="TMFH6")
    mon.far_contract = SimpleNamespace(code="TMFI6")
    mon.order_mgr = SimpleNamespace(completed=completed_orders)
    mon.ticker = "TMF"
    mon._capture_post_startup_snapshot = lambda: snapshot
    mon._persist_current_session_canonical = lambda snap: None
    mon._reconcile_local_orders_from_snapshot = lambda snap: 0
    mon._write_live_session_upl = lambda rows, ctx: None
    mon._stable_broker_trade_id = lambda *a, **k: "stable-1"
    return mon


def _ok_snapshot(open_orders, positions=()):
    return {
        "source": "live_broker",
        "fetch_status": {"capture": "OK"},
        "positions": list(positions),
        "open_orders": list(open_orders),
        "account_identity_hash": "acct",
        "canonical_input_hash": "hash1",
    }


def _pending(**kw):
    base = {"broker_order_id": "B-1", "ordno": "B-1", "seqno": "S-1",
            "code": "TMFH6", "status": "PendingSubmit",
            "direction": "Action.Sell", "quantity": 1}
    base.update(kw)
    return base


class _FilledOrder:
    def __init__(self, **kw):
        base = {"order_id": "ORD-1", "symbol": "TMFH6", "side": "sell",
                "quantity": 1, "status": "filled", "broker_order_id": "B-1",
                "exchange_order_id": "B-1", "ordno": "B-1", "seqno": "S-1",
                "strategy": "MTS_ENTRY"}
        base.update(kw)
        self._d = base

    def to_dict(self):
        return dict(self._d)


class _Strategy:
    def __init__(self):
        self._broker_truth_flat = None
        self._has_position = True


def test_covered_stale_pending_resolves_flat():
    snap = _ok_snapshot(open_orders=[
        _pending(broker_order_id="B-1", code="TMFH6", direction="Action.Sell",
                 quantity=1),
        _pending(broker_order_id="B-2", code="TMFI6", direction="Action.Buy",
                 quantity=1),
    ])
    filled = [
        _FilledOrder(order_id="ORD-1", symbol="TMFH6", side="sell",
                     quantity=1, broker_order_id="B-1"),
        _FilledOrder(order_id="ORD-2", symbol="TMFI6", side="buy",
                     quantity=1, broker_order_id="B-2"),
    ]
    strat = _Strategy()
    auth = _monitor(snap, filled)._refresh_live_broker_authority(strat)
    assert auth is not None
    assert auth.status == MtsAuthority.FLAT
    assert strat._broker_truth_flat is True
    assert strat._has_position is True  # strategy memory untouched here


def test_uncovered_pending_keeps_unresolved():
    # one open order has NO local filled evidence -> never flat
    snap = _ok_snapshot(open_orders=[
        _pending(broker_order_id="B-9", code="TMFH6", direction="Action.Sell",
                 quantity=1),
    ])
    strat = _Strategy()
    mon = _monitor(snap, [])
    auth = mon._refresh_live_broker_authority(strat)
    assert auth is None
    assert strat._broker_truth_flat is False
    assert mon._live_broker_flat_proven is False


def test_true_pending_exit_not_covered_by_entry_fills():
    # entry filled (B-1) + genuinely pending exit (B-9, reverse dir) -> unresolved
    snap = _ok_snapshot(open_orders=[
        _pending(broker_order_id="B-1", code="TMFH6", direction="Action.Sell",
                 quantity=1),
        _pending(broker_order_id="B-9", code="TMFH6", direction="Action.Buy",
                 quantity=1),
    ])
    filled = [_FilledOrder(order_id="ORD-1", symbol="TMFH6", side="sell",
                           quantity=1, broker_order_id="B-1")]
    strat = _Strategy()
    auth = _monitor(snap, filled)._refresh_live_broker_authority(strat)
    assert auth is None
    assert strat._broker_truth_flat is False


def test_empty_snapshot_no_open_orders_still_flat():
    snap = _ok_snapshot(open_orders=[])
    strat = _Strategy()
    auth = _monitor(snap, [])._refresh_live_broker_authority(strat)
    assert auth is not None
    assert auth.status == MtsAuthority.FLAT
