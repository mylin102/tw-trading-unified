"""RED: canonical flat / non-TMF-only must be authoritative.

capture == OK (even with positions=[] or no TMF contracts) is ALWAYS
authoritative — zero close info, zero orders, NO fallback to local
state even when local state claims a (stale ghost) position.
Local state is used ONLY when the capture is unavailable.
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


def _stale_local_position(tmp_path, monkeypatch):
    """Local state claims a ghost position (stale)."""
    st = tmp_path / "mts_position_state.json"
    st.write_text(json.dumps({
        "has_position": True, "near_side": "SHORT", "far_side": "LONG",
        "released_leg": None, "trade_id": "ghost-trade",
    }), encoding="utf-8")
    monkeypatch.setenv("MTS_STATE_PATH", str(st))


def test_canonical_flat_overrides_stale_local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    _stale_local_position(tmp_path, monkeypatch)

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "OK"}, "positions": [], "open_orders": []}

    r = monitor._resolve_close_all_position()

    assert r[0] is False          # zero close info
    assert r[1] is None and r[2] is None
    assert r[3] is None


def test_canonical_non_tmf_only_overrides_stale_local(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    _stale_local_position(tmp_path, monkeypatch)

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "OK"},
        "positions": [
            {"account": "futures", "code": "MXF", "quantity": 1,
             "direction": "Action.Buy", "avg_cost": 100.0},
        ],
        "open_orders": []}

    r = monitor._resolve_close_all_position()

    assert r[0] is False          # no TMF legs -> authoritative flat
    assert r[1] is None and r[2] is None
