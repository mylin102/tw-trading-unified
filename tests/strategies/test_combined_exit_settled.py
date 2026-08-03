# Phase B tests: COMBINED_EXIT_SETTLED canonical settlement event.
import json
from unittest.mock import MagicMock, patch

import pytest

from strategies.futures.monitor import FuturesMonitor


def _make_monitor(tmp_path):
    with patch.object(FuturesMonitor, '__init__', lambda self: None):
        mon = FuturesMonitor()
        mon.contract = MagicMock(code="TMFH6")
        mon.far_contract = MagicMock(code="TMFI6")
        mon.ticker = "TMF"
        mon.cfg = {"point_value": 10}
        mon.dry_run = True
        mon.live_trading = False
        mon.trader = MagicMock(position=0)
        mon.order_mgr = MagicMock()
        mon._pending_lifecycle_orders = {}
        mon._combined_exit_trackers = {}
        from strategies.plugins.futures.active import tmf_spread
        mon._fill_log = str(tmp_path / "fills.jsonl")
        p = patch.object(tmf_spread, "_MTS_FILL_LOG", mon._fill_log)
        p.start()
        mon._fill_log_patch = p
        return mon


def _state_file(tmp_path, near_side="LONG", far_side="SHORT",
                near_entry=43770.0, far_entry=43914.0):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"near_side": near_side, "far_side": far_side,
                             "near_entry": near_entry, "far_entry": far_entry,
                             "state": "FLAT", "has_position": False}))
    return p


def _fe(order_id, qty=1, price=44000.0):
    import types
    return types.SimpleNamespace(order_id=order_id, fill_qty=qty, fill_price=price,
                                 deal_id=f"deal_{order_id}", symbol=None)


def _both(mon, tid, np_, fp_, sp):
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path", return_value=sp):
        mon._apply_combined_exit_fill(_fe("O-N", price=np_), {"trade_id": tid, "lots": 1, "strategy": "MTS_EXIT"}, "COMBINED_EXIT_NEAR", np_)
        mon._apply_combined_exit_fill(_fe("O-F", price=fp_), {"trade_id": tid, "lots": 1, "strategy": "MTS_EXIT"}, "COMBINED_EXIT_FAR", fp_)


def _rows(tmp_path):
    p = tmp_path / "fills.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().strip().splitlines() if l.strip()]


def _settled(rows):
    return [r for r in rows if r.get("event_type") == "COMBINED_EXIT_SETTLED"]


@pytest.fixture(autouse=True)
def _pp(tmp_path):
    with patch("strategies.futures.monitor._mts_position_state_path") as mp:
        mp.return_value.exists.return_value = False
        yield mp


def test_settled_emitted_once_after_both_legs(tmp_path):
    mon = _make_monitor(tmp_path)
    _both(mon, "T1", 43825.0, 43910.0, _state_file(tmp_path))
    import os as _os
    _p = tmp_path / "fills.jsonl"
    print(f"\nDEBUG path={_p} exists={_os.path.exists(_p)}")
    if _os.path.exists(_p):
        for _l in _p.read_text().splitlines():
            import json as _j
            try:
                _d = _j.loads(_l)
                print("DEBUG row:", _d.get("event_type") or _d.get("fill_type"), _d.get("trade_id"))
            except Exception:
                pass
    rows = _rows(tmp_path)
    s = _settled(rows)
    assert len(s) == 1, f"exactly one SETTLED, got {len(s)}"


def test_first_leg_no_settled(tmp_path):
    mon = _make_monitor(tmp_path)
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path",
               return_value=_state_file(tmp_path)):
        mon._apply_combined_exit_fill(_fe("O-N", price=43825.0),
                                      {"trade_id": "T2", "lots": 1, "strategy": "MTS_EXIT"},
                                      "COMBINED_EXIT_NEAR", 43825.0)
    assert _settled(_rows(tmp_path)) == [], "first leg must not settle"


def test_duplicate_callback_no_second_settled(tmp_path):
    mon = _make_monitor(tmp_path)
    _both(mon, "T3", 43825.0, 43910.0, _state_file(tmp_path))
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path",
               return_value=_state_file(tmp_path)):
        mon._apply_combined_exit_fill(_fe("O-F", price=43910.0),
                                      {"trade_id": "T3", "lots": 1, "strategy": "MTS_EXIT"},
                                      "COMBINED_EXIT_FAR", 43910.0)
    assert len(_settled(_rows(tmp_path))) == 1, "duplicate must not re-settle"


def test_restart_no_duplicate_settled(tmp_path):
    """Replay (new tracker from fills log) must not emit a second SETTLED."""
    mon = _make_monitor(tmp_path)
    _both(mon, "T4", 43825.0, 43910.0, _state_file(tmp_path))
    assert len(_settled(_rows(tmp_path))) == 1
    # simulate restart: fresh tracker instance, same fills log (SETTLED present)
    mon2 = _make_monitor(tmp_path)
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path",
               return_value=_state_file(tmp_path)):
        # rebuild tracker from fills then re-apply far fill
        from strategies.futures.monitor import FuturesMonitor as FM
        tr = mon2._get_combined_exit_tracker("T4")
        # fills log already has SETTLED -> dedupe scan blocks re-emission
        mon2._apply_combined_exit_fill(_fe("O-F", price=43910.0),
                                       {"trade_id": "T4", "lots": 1, "strategy": "MTS_EXIT"},
                                       "COMBINED_EXIT_FAR", 43910.0)
    assert len(_settled(_rows(tmp_path))) == 1, "restart replay must not re-settle"


def test_leg_pnl_sum_equals_combined_gross(tmp_path):
    mon = _make_monitor(tmp_path)
    _both(mon, "T5", 43825.0, 43910.0, _state_file(tmp_path))
    s = _settled(_rows(tmp_path))[0]
    assert s["combined_realized_pnl_gross"] == pytest.approx(
        s["near_realized_pnl_gross"] + s["far_realized_pnl_gross"], abs=0.2)


def test_net_null_when_no_fees(tmp_path):
    mon = _make_monitor(tmp_path)
    _both(mon, "T6", 43825.0, 43910.0, _state_file(tmp_path))
    s = _settled(_rows(tmp_path))[0]
    assert s["combined_realized_pnl_net"] is None
    assert s["pnl_status"] == "GROSS_ONLY"
    assert s["fees"] is None and s["tax"] is None


def test_origin_live_and_confidence_exact(tmp_path):
    mon = _make_monitor(tmp_path)
    _both(mon, "T7", 43825.0, 43910.0, _state_file(tmp_path))
    s = _settled(_rows(tmp_path))[0]
    assert s["settlement_origin"] == "LIVE"
    assert s["price_confidence"] == "EXACT"


def test_settled_carries_contracts_and_entry_exit(tmp_path):
    mon = _make_monitor(tmp_path)
    _both(mon, "T8", 43825.0, 43910.0, _state_file(tmp_path))
    s = _settled(_rows(tmp_path))[0]
    assert s["near_contract"] == "TMFH6"
    assert s["far_contract"] == "TMFI6"
    assert s["near_entry_avg_price"] == 43770.0
    assert s["near_exit_avg_price"] == 43825.0
    assert s["near_closed_qty"] == 1
    assert s["combined_exit_id"] == "T8"
    assert s["trade_id"] == "T8"
