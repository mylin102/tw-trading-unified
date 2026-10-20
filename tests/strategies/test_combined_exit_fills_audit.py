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




def _complete_both(mon, tmp_path, trade_id, near_px, far_px, sp, qty=1):
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path", return_value=sp):
        mon._apply_combined_exit_fill(_fill_event("O-N", qty=qty, price=near_px),
                                      {"trade_id": trade_id, "lots": qty, "strategy": "MTS_EXIT"},
                                      "COMBINED_EXIT_NEAR", near_px)
        mon._apply_combined_exit_fill(_fill_event("O-F", qty=qty, price=far_px),
                                      {"trade_id": trade_id, "lots": qty, "strategy": "MTS_EXIT"},
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
    # 2026-10-20 Codex: Hermes-approved contract requires net exit PnL and explicit cost components.
    gross = (43825.0 - 43770.0) * 1 * 10
    fee = 40.0
    tax = (43770.0 + 43825.0) * 10 * 1 * 2e-5
    assert near["gross_pnl"] == pytest.approx(gross)
    assert near["fee"] == pytest.approx(fee)
    assert near["tax"] == pytest.approx(tax)
    assert near["realized_pnl"] == pytest.approx(round(gross - fee - tax, 1))


def test_short_pnl_sign_and_value(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path, near_side="SHORT", far_side="LONG",
                     near_entry=43914.0, far_entry=43770.0)
    _complete_both(mon, tmp_path, "T5", 43825.0, 43900.0, sp)
    near = [f for f in _read_fills(tmp_path) if f.get("leg") == "NEAR"][0]
    gross = (43914.0 - 43825.0) * 1 * 10
    fee = 40.0
    tax = (43914.0 + 43825.0) * 10 * 1 * 2e-5
    assert near["gross_pnl"] == pytest.approx(gross)
    assert near["fee"] == pytest.approx(fee)
    assert near["tax"] == pytest.approx(tax)
    assert near["realized_pnl"] == pytest.approx(round(gross - fee - tax, 1))


def test_combined_exit_matches_strategy_exit_friction_formula_for_multiple_lots(tmp_path):
    mon = _make_monitor(tmp_path)
    near_entry, near_exit, qty = 43770.0, 43825.0, 2
    sp = _state_file(tmp_path, near_side="LONG", far_side="SHORT",
                     near_entry=near_entry, far_entry=43914.0)
    _complete_both(mon, tmp_path, "T-PARITY", near_exit, 43800.0, sp, qty=qty)
    near = next(f for f in _read_fills(tmp_path)
                if f.get("fill_type") == "COMBINED_EXIT" and f.get("leg") == "NEAR")

    # Same arithmetic as the existing EXIT path: gross - (40 + turnover * 2e-5), per lot.
    gross = (near_exit - near_entry) * 10 * qty
    fee = 40.0 * qty
    tax = (near_entry + near_exit) * 10 * qty * 2e-5
    strategy_exit_net = round(gross - fee - tax, 1)
    assert near["gross_pnl"] == pytest.approx(gross)
    assert near["fee"] == pytest.approx(fee)
    assert near["tax"] == pytest.approx(tax)
    assert near["realized_pnl"] == strategy_exit_net


def test_combined_exit_zero_qty_has_zero_friction(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path)
    tracker = mon._get_combined_exit_tracker("T-ZERO")
    tracker.update({
        "near_side": "LONG", "far_side": "SHORT",
        "near_entry": 43770.0, "far_entry": 43914.0,
        "near_price": 43825.0, "far_price": 43800.0,
        "near_expected_qty": 0, "far_expected_qty": 0,
        "near_filled_qty": 0, "far_filled_qty": 0,
        "near_complete": True, "far_complete": True,
    })
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path", return_value=sp):
        mon._apply_combined_exit_fill(_fill_event("O-Z", qty=0, price=43825.0),
                                      {"trade_id": "T-ZERO", "lots": 0, "strategy": "MTS_EXIT"},
                                      "COMBINED_EXIT_NEAR", 43825.0)
    near = next(f for f in _read_fills(tmp_path)
                if f.get("fill_type") == "COMBINED_EXIT" and f.get("leg") == "NEAR")
    assert near["qty"] == 0
    assert near["gross_pnl"] == 0
    assert near["fee"] == 0
    assert near["tax"] == 0
    assert near["realized_pnl"] == 0


def test_combined_settlement_records_net_and_total_costs(tmp_path):
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path, near_side="LONG", far_side="SHORT",
                     near_entry=43770.0, far_entry=43914.0)
    _complete_both(mon, tmp_path, "T-SETTLED-NET", 43825.0, 43800.0, sp)
    fills = _read_fills(tmp_path)
    exits = [f for f in fills if f.get("fill_type") == "COMBINED_EXIT"]
    settled = next(f for f in fills if f.get("fill_type") == "COMBINED_EXIT_SETTLED")
    assert len(exits) == 2
    assert settled["pnl_status"] == "NET"
    assert settled["combined_realized_pnl_net"] == pytest.approx(
        sum(f["realized_pnl"] for f in exits))
    assert settled["fees"] == pytest.approx(sum(f["fee"] for f in exits))
    assert settled["tax"] == pytest.approx(sum(f["tax"] for f in exits))


def test_price_source_broker_fill(tmp_path):
    """Paper simulator fills retain paper provenance, not broker provenance."""
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path)
    _complete_both(mon, tmp_path, "T6", 43825.0, 43910.0, sp)
    near = [f for f in _read_fills(tmp_path) if f.get("leg") == "NEAR"][0]
    assert near["price_source"] == "PAPER_FILL"


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


def test_existing_settled_row_does_not_block_missing_leg_fills(tmp_path):
    # 2026-10-20 Hermes Agent spec, implemented by Codex: cover crash after SETTLED but before leg rows.
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path)
    with open(mon._fill_log, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "event_type": "COMBINED_EXIT_SETTLED", "trade_id": "T-CRASH-SETTLED",
        }) + "\n")
    persistence_failed = MagicMock()
    mon._enter_settlement_persistence_failed = persistence_failed

    _complete_both(mon, tmp_path, "T-CRASH-SETTLED", 43825.0, 43910.0, sp)

    fills = _read_fills(tmp_path)
    legs = [f for f in fills if f.get("fill_type") == "COMBINED_EXIT"]
    assert len(legs) == 2
    assert all({"gross_pnl", "fee", "tax"} <= f.keys() for f in legs)
    assert all(f["realized_pnl"] == round(
        f["gross_pnl"] - f["fee"] - f["tax"], 1) for f in legs)
    assert not persistence_failed.called
    assert mon._combined_exit_trackers["T-CRASH-SETTLED"]["settlement_completed"] is True


def test_settled_log_read_error_still_writes_net_leg_fills(tmp_path):
    # 2026-10-20 Hermes Agent spec, implemented by Codex: leg settlement survives SETTLED scan errors.
    mon = _make_monitor(tmp_path)
    sp = _state_file(tmp_path)
    with open(mon._fill_log, "w", encoding="utf-8") as f:
        f.write("{}\n")
    real_open = open

    def _open(path, mode="r", *args, **kwargs):
        if os.fspath(path) == mon._fill_log and mode == "r":
            raise OSError("simulated SETTLED log read failure")
        return real_open(path, mode, *args, **kwargs)

    persistence_failed = MagicMock()
    mon._enter_settlement_persistence_failed = persistence_failed
    with patch("builtins.open", side_effect=_open):
        _complete_both(mon, tmp_path, "T-SETTLED-READ-ERROR", 43825.0, 43910.0, sp)

    fills = _read_fills(tmp_path)
    legs = [f for f in fills if f.get("fill_type") == "COMBINED_EXIT"]
    assert len(legs) == 2
    assert all({"gross_pnl", "fee", "tax"} <= f.keys() for f in legs)
    assert all(f["realized_pnl"] == round(
        f["gross_pnl"] - f["fee"] - f["tax"], 1) for f in legs)
    assert not persistence_failed.called
    assert mon._combined_exit_trackers["T-SETTLED-READ-ERROR"]["settlement_completed"] is True
