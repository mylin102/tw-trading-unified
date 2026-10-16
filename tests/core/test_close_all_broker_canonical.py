"""RED: close_all resolves sides from the BROKER canonical when the
local state is empty (single-leg residual scenario).

Broker facts are the authority: the emergency close must flatten
whatever the broker actually holds — two legs or the remaining single
leg — even when mts_position_state.json is empty.
"""

import json
from types import SimpleNamespace


def _monitor(tmp_path):
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._execution_context = SimpleNamespace(
        requested_mode="live", effective_mode="live_ready", session_id="sess")
    return monitor


def test_close_all_resolves_single_leg_from_broker_canonical(
        tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    # empty local state file
    st = tmp_path / "mts_position_state.json"
    st.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MTS_STATE_PATH", str(st))

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "OK"},
        "positions": [
            {"account": "futures", "code": "TMFI6", "quantity": 1,
             "direction": "Action.Buy", "avg_cost": 46033.0},
        ],
        "open_orders": [],
    }

    _has_pos, _near_side, _far_side, _released_leg, _tid, _disk = \
        monitor._resolve_close_all_position()

    assert _has_pos is True
    assert _near_side is None
    assert _far_side == "LONG"       # the remaining leg from broker facts
    assert _released_leg == "near"   # near was released; far remains


def test_close_all_resolves_both_legs_from_broker_canonical(
        tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    st = tmp_path / "mts_position_state.json"
    st.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MTS_STATE_PATH", str(st))

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "OK"},
        "positions": [
            {"account": "futures", "code": "TMFH6", "quantity": 1,
             "direction": "Action.Sell", "avg_cost": 45879.0},
            {"account": "futures", "code": "TMFI6", "quantity": 1,
             "direction": "Action.Buy", "avg_cost": 46033.0},
        ],
        "open_orders": [],
    }

    _has_pos, _near_side, _far_side, _released_leg, _tid, _disk = \
        monitor._resolve_close_all_position()

    assert _has_pos is True
    assert _near_side == "SHORT" and _far_side == "LONG"
    assert _released_leg is None
