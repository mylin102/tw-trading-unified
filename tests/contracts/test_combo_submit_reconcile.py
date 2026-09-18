"""RED contract for live combo submission reconciliation.

The gateway receipt is not broker proof.  A combo entry must be reconciled
against the authenticated broker order/trade view before local legs can be
retained.
"""

import json
from types import SimpleNamespace


def _monitor(tmp_path):
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor.dry_run = False
    monitor._mts_pending_fills = {
        "T-1818": {
            "combo": True,
            "combo_order_id": "LOCAL-47",
            "combo_legs": [
                {"symbol": "TMFJ6", "quantity": 1},
                {"symbol": "TMFK6", "quantity": 1},
            ],
        }
    }
    order = SimpleNamespace(
        order_id="LOCAL-47", broker_order_id="1818c426",
        exchange_order_id="1818c426", seqno=None, ordno=None,
    )
    monitor.order_mgr = SimpleNamespace(
        active_orders={"LOCAL-47": order}, completed=[],
    )
    monitor._pending_lifecycle_orders = {
        "LOCAL-47": {"combo": True, "trade_id": "T-1818"}
    }
    state = tmp_path / "mts_state.json"
    state.write_text(json.dumps({
        "has_position": True, "near_side": "SHORT", "far_side": "LONG",
        "near_entry": 47279, "far_entry": 47444,
        "reason": "mts_sync_status",
    }))
    return monitor, state


def test_missing_broker_combo_is_terminal_and_clears_local_legs(tmp_path, monkeypatch):
    from strategies.futures import monitor as monitor_module

    monitor, state = _monitor(tmp_path)
    monkeypatch.setattr(monitor_module, "_mts_position_state_path",
                        lambda: state)
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "OK"},
        "positions": [], "open_orders": [], "broker_trades": [],
    }
    monitor._append_mts_event = lambda *args, **kwargs: None

    monitor._reconcile_pending_mts_combos()

    assert monitor.order_mgr.active_orders == {}
    assert monitor.order_mgr.completed[0].status.value == "BROKER_NOT_FOUND"
    assert monitor._mts_pending_fills == {}
    assert monitor._pending_lifecycle_orders == {}
    saved = json.loads(state.read_text())
    assert saved["has_position"] is False
    assert not any(saved.get(key) for key in (
        "near_side", "far_side", "near_entry", "far_entry"))


def test_combo_submission_requires_broker_presence_before_state_promotion():
    from pathlib import Path
    import ast

    source = Path("strategies/futures/monitor.py").read_text()
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_submit_mts_combo_entry")
    names = [n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "_reconcile_pending_mts_combos" in names
