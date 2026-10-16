"""2026-10-06 Codex: real confirmed-fill callsite, isolated PAPER ledger."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.order_management.order import OrderStatus
from strategies.futures.monitor import FuturesMonitor
from strategies.plugins.futures.active import tmf_spread


def test_distinct_leg_callbacks_settle_once_under_interleaving(tmp_path, monkeypatch):
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.contract = SimpleNamespace(code="TMFJ6")
    mon.far_contract = SimpleNamespace(code="TMFK6")
    mon.ticker, mon.cfg = "TMF", {"point_value": 10}
    mon.live_trading, mon.dry_run = False, True
    mon.trader, mon.order_mgr = MagicMock(position=0), MagicMock()
    mon._registry = {}
    mon._combined_exit_trackers = {}
    mon._claimed_execution_keys, mon._applied_lifecycle_deals = set(), set()
    mon._execution_context = SimpleNamespace(effective_mode="paper_active")
    trade = "mts-auto-134000-323"
    mon._pending_lifecycle_orders = {
        "O-N": {"trade_id": trade, "lots": 1, "strategy": "MTS_EXIT",
                "signal": "COMBINED_EXIT_NEAR", "reason": "COMBINED_EXIT"},
        "O-F": {"trade_id": trade, "lots": 1, "strategy": "MTS_EXIT",
                "signal": "COMBINED_EXIT_FAR", "reason": "COMBINED_EXIT"},
    }
    # Only unrelated IO/entry/reconciliation boundaries are mocked; neither
    # confirmed-fill dispatch nor settlement nor the append implementation is.
    for name in ("_check_mts_multi_leg_fill", "_maybe_complete_emergency_command",
                 "_save_orders_file_wrapper", "_append_mts_event",
                 "_enter_post_exit_reconciliation"):
        monkeypatch.setattr(mon, name, MagicMock())
    monkeypatch.setattr(mon, "_get_lifecycle_order", lambda oid: None)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"near_side": "LONG", "far_side": "SHORT",
                                 "near_entry": 49915, "far_entry": 50094}))
    fills = tmp_path / "fills.jsonl"
    monkeypatch.setattr(tmf_spread, "_get_state_file_path", lambda: str(state))
    monkeypatch.setattr(tmf_spread, "_MTS_FILL_LOG", str(fills))
    tracker = mon._get_combined_exit_tracker(trade)
    tracker.update(near_expected_qty=1, far_expected_qty=1)

    # Schedule both actual leg callbacks at the two non-atomic boundaries.
    # Bounded barriers also permit the correct serialized implementation to
    # finish: its first callback cannot wait indefinitely for the locked leg.
    updated, committing = threading.Barrier(2), threading.Barrier(2)
    real_append = tmf_spread._append_fill

    def wait(barrier):
        try:
            barrier.wait(timeout=0.3)
        except threading.BrokenBarrierError:
            pass

    def print_boundary(message, *args, **kwargs):
        if "[COMBINED_EXIT_FILL]" in str(message):
            wait(updated)

    def append_boundary(*args, **kwargs):
        if kwargs.get("fill_type") == "COMBINED_EXIT_SETTLED":
            wait(committing)
        return real_append(*args, **kwargs)

    monkeypatch.setattr("strategies.futures.monitor.console.print", print_boundary)
    monkeypatch.setattr(tmf_spread, "_append_fill", append_boundary)
    errors = []

    def callback(oid, price):
        try:
            mon._apply_confirmed_futures_deal(SimpleNamespace(
                order_id=oid, fill_qty=1, fill_price=price,
                deal_id="PAPER-" + oid + "-1", symbol="TMFJ6" if oid == "O-N" else "TMFK6",
                status=OrderStatus.FILLED))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=callback, args=(oid, px))
               for oid, px in (("O-N", 50068), ("O-F", 50264))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    rows = [json.loads(line) for line in fills.read_text().splitlines()]
    assert [r["fill_type"] for r in rows].count("COMBINED_EXIT_SETTLED") == 1
    exits = [r for r in rows if r["fill_type"] == "COMBINED_EXIT"]
    assert [(r["leg"], r["side"], r["qty"]) for r in exits] == [
        ("NEAR", "SELL", 1), ("FAR", "BUY", 1)]
    assert sum(r["realized_pnl"] for r in exits) == -170
    assert [r["fill_type"] for r in rows].count("COMBINED_EXIT_COMPLETED") == 1
    assert tracker["settlement_completed"] is True
    assert mon._enter_post_exit_reconciliation.call_count == 1
    assert mon.trader.client.mock_calls == []  # no broker authority for this test
