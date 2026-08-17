"""RED: close_all broker-canonical hardening (codex gaps).

1. Stale NON-EMPTY local state must NOT override the broker canonical —
   broker facts are the authority (canonical first, local fallback only).
2. Duplicate contracts or unknown directions in the canonical must
   fail-closed (no close info) instead of being folded by _by_code.
3. Dashboard freshness must reject future timestamps and unify
   timezone-aware timestamps.
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


def _snap(positions):
    return {"fetch_status": {"capture": "OK"}, "positions": positions,
            "open_orders": []}


def test_stale_local_state_does_not_override_broker_canonical(
        tmp_path, monkeypatch):
    """Local state says near=LONG/far=SHORT (stale/wrong); canonical says
    TMFH6 SHORT + TMFI6 LONG — the canonical must win."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    st = tmp_path / "mts_position_state.json"
    st.write_text(json.dumps({
        "has_position": True, "near_side": "LONG", "far_side": "SHORT",
        "released_leg": None, "trade_id": "stale-trade",
    }), encoding="utf-8")
    monkeypatch.setenv("MTS_STATE_PATH", str(st))

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap([
        {"account": "futures", "code": "TMFH6", "quantity": 1,
         "direction": "Action.Sell", "avg_cost": 45879.0},
        {"account": "futures", "code": "TMFI6", "quantity": 1,
         "direction": "Action.Buy", "avg_cost": 46033.0},
    ])

    _has_pos, _near_side, _far_side, _released_leg, _tid, _disk = \
        monitor._resolve_close_all_position()

    assert _has_pos is True
    assert _near_side == "SHORT" and _far_side == "LONG"  # canonical wins
    assert _released_leg is None


def test_duplicate_contract_in_canonical_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    st = tmp_path / "mts_position_state.json"
    st.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MTS_STATE_PATH", str(st))

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap([
        {"account": "futures", "code": "TMFI6", "quantity": 1,
         "direction": "Action.Buy", "avg_cost": 46033.0},
        {"account": "futures", "code": "TMFI6", "quantity": 1,
         "direction": "Action.Buy", "avg_cost": 46030.0},
    ])

    r = monitor._resolve_close_all_position()
    # fail-closed: has_pos False -> close_all refuses (no invalid guess)
    assert r[0] is False


def test_unknown_direction_in_canonical_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    st = tmp_path / "mts_position_state.json"
    st.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MTS_STATE_PATH", str(st))

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap([
        {"account": "futures", "code": "TMFI6", "quantity": 1,
         "direction": "Action.Unknown", "avg_cost": 46033.0},
    ])

    r = monitor._resolve_close_all_position()
    assert r[0] is False


def test_freshness_rejects_future_timestamp():
    import datetime
    from ui.dashboard import _manual_command_is_fresh

    future = (datetime.datetime.now() + datetime.timedelta(minutes=10)).isoformat()
    assert _manual_command_is_fresh({"ts": future}) is False


def test_freshness_accepts_tz_aware_recent():
    import datetime
    from ui.dashboard import _manual_command_is_fresh

    recent = datetime.datetime.now(datetime.timezone.utc).isoformat()
    assert _manual_command_is_fresh({"ts": recent}) is True
