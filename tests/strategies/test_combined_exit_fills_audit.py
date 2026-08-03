# COMBINED_EXIT fills audit tests (2026-08-03 fix):
# exit-side mapping, canonical per-leg price, realized PnL, provenance fields.
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from strategies.futures.monitor import FuturesMonitor

sys_path_fix = None  # noqa


def _make_monitor(tmp_path):
    with patch.object(FuturesMonitor, '__init__', lambda self: None):
        mon = FuturesMonitor()
        mon.contract = MagicMock(code="TMFH6")
        mon.far_contract = MagicMock(code="TMFI6")
        mon.ticker = "TMF"
        mon.cfg = {"point_value": 10}
        mon.dry_run = True
        mon.live_trading = False
        mon.trader = MagicMock(position=0)  # reconciliation broker query
        mon.order_mgr = MagicMock()
        mon._pending_lifecycle_orders = {}
        mon._combined_exit_trackers = {}
        mon._claimed_execution_keys = set()
        # fill log → tmp
        from strategies.plugins.futures.active import tmf_spread
        mon._fill_log = str(tmp_path / "fills.jsonl")
        mon_fill_log_patch = patch.object(tmf_spread, "_MTS_FILL_LOG", mon._fill_log)
        mon_fill_log_patch.start()
        mon._fill_log_patch = mon_fill_log_patch
        return mon


def _state_file(tmp_path, near_side="LONG", far_side="SHORT",
                near_entry=43770.0, far_entry=43914.0):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "near_side": near_side, "far_side": far_side,
        "near_entry": near_entry, "far_entry": far_entry,
        "state": "FLAT", "has_position": False,
    }))
    return p


def _fill_event(order_id, qty=1, price=44000.0):
    import types
    ev = types.SimpleNamespace(order_id=order_id, fill_qty=qty, fill_price=price,
                               deal_id=f"deal_{order_id}", symbol=None)
    return ev




def _complete_both(mon, tmp_path, trade_id, near_px, far_px, sp):
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path", return_value=sp):
        mon._apply_combined_exit_fill(_fill_event("O-N", qty=1, price=near_px),
                                      {"trade_id": trade_id, "lots": 1, "strategy": "MTS_EXIT"},
                                      "COMBINED_EXIT_NEAR", near_px)
        mon._apply_combined_exit_fill(_fill_event("O-F", qty=1, price=far_px),
                                      {"trade_id": trade_id, "lots": 1, "strategy": "MTS_EXIT"},
                                      "COMBINED_EXIT_FAR", far_px)

def _read_fills(tmp_path):
    p = tmp_path / "fills.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().strip().splitlines() if l.strip()]


@pytest.fixture(autouse=True)
def _patch_state_path(tmp_path):
    with patch("strategies.futures.monitor._mts_position_state_path") as mp:
        mp.return_value.exists.return_value = False
        yield mp


def test_long_exit_fill_side_is_sell(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path, near_side="LONG", far_side="SHORT")
    _complete_both(mon, tmp_path, "T1", 43825.0, 43910.0, sp)
    fills = _read_fills(tmp_path)
    near_fills = [f for f in fills if f.get("leg") == "NEAR"]
    assert near_fills, "no NEAR fill written"
    assert near_fills[0]["side"] == "SELL", "LONG exit must be SELL"
    assert near_fills[0]["position_side_before_exit"] == "LONG"
    assert near_fills[0]["position_effect"] == "CLOSE"


def test_short_exit_fill_side_is_buy(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path, near_side="SHORT", far_side="LONG")
    _complete_both(mon, tmp_path, "T2", 43800.0, 43650.0, sp)
    fills = _read_fills(tmp_path)
    near_fills = [f for f in fills if f.get("leg") == "NEAR"]
    assert near_fills[0]["side"] == "BUY", "SHORT exit must be BUY"


def test_per_leg_price_not_shared(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path)
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path", return_value=sp):
        mon._apply_combined_exit_fill(_fill_event("O1", price=43825.0),
                                      {"trade_id": "T3", "lots": 1, "strategy": "MTS_EXIT"},
                                      "COMBINED_EXIT_NEAR", 43825.0)
        mon._apply_combined_exit_fill(_fill_event("O2", price=43910.0),
                                      {"trade_id": "T3", "lots": 1, "strategy": "MTS_EXIT"},
                                      "COMBINED_EXIT_FAR", 43910.0)
    fills = _read_fills(tmp_path)
    near = [f for f in fills if f.get("leg") == "NEAR"][0]
    far = [f for f in fills if f.get("leg") == "FAR"][0]
    assert near["price"] == 43825.0
    assert far["price"] == 43910.0
    assert near["price"] != far["price"]


def test_long_pnl_sign_and_value(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path, near_side="LONG", far_side="SHORT",
                     near_entry=43770.0, far_entry=43914.0)
    _complete_both(mon, tmp_path, "T4", 43825.0, 43800.0, sp)
    near = [f for f in _read_fills(tmp_path) if f.get("leg") == "NEAR"][0]
    assert near["realized_pnl"] == pytest.approx((43825.0 - 43770.0) * 1 * 10, abs=0.2)


def test_short_pnl_sign_and_value(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path, near_side="SHORT", far_side="LONG",
                     near_entry=43914.0, far_entry=43770.0)
    _complete_both(mon, tmp_path, "T5", 43825.0, 43900.0, sp)
    near = [f for f in _read_fills(tmp_path) if f.get("leg") == "NEAR"][0]
    assert near["realized_pnl"] == pytest.approx((43914.0 - 43825.0) * 1 * 10, abs=0.2)


def test_price_source_broker_fill(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path)
    _complete_both(mon, tmp_path, "T6", 43825.0, 43910.0, sp)
    near = [f for f in _read_fills(tmp_path) if f.get("leg") == "NEAR"][0]
    assert near["price_source"] == "BROKER_FILL"


def test_duplicate_settlement_suppressed(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path)
    _complete_both(mon, tmp_path, "T7", 43825.0, 43910.0, sp)
    # duplicate far callback → settlement already completed → no extra fills
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path", return_value=sp):
        mon._apply_combined_exit_fill(_fill_event("O2", price=43910.0),
                                      {"trade_id": "T7", "lots": 1, "strategy": "MTS_EXIT"},
                                      "COMBINED_EXIT_FAR", 43910.0)
    fills = _read_fills(tmp_path)
    ce_fills = [f for f in fills if f.get("fill_type") == "COMBINED_EXIT"]
    assert len(ce_fills) == 2, f"duplicate callback must not add fills: {len(ce_fills)}"


def test_real_trade_id_preserved(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path)
    _complete_both(mon, tmp_path, "mts-auto-123456-789", 43825.0, 43910.0, sp)
    near = [f for f in _read_fills(tmp_path) if f.get("leg") == "NEAR"][0]
    assert near["trade_id"] == "mts-auto-123456-789"
