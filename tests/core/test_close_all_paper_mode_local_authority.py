"""2026-11-24 Hermes Agent: close_all must resolve from LOCAL PAPER STATE in
paper mode, never from the broker canonical.

A paper position never exists at the real broker. Before this fix,
_resolve_close_all_position() queried the broker canonical first in EVERY
mode; when it legitimately returned OK with an empty/no-TMF positions list
(normal for a paper trade), the function treated that as authoritative FLAT
and returned immediately -- silently no-opping 緊急全平倉 on a real open
paper position (observed live: command returned COMPLETED with
"無持倉可平" while mts_position_state.json still showed has_position=True
and a large open near-leg UPL).

LIVE mode's anti-ghost-resurrection invariant (empty broker canonical always
wins over a stale local state) must be completely unaffected -- see
test_close_all_canonical_flat.py / test_close_all_broker_canonical.py /
test_close_all_hardening.py, all of which pin live_trading=True.
"""

import json
from types import SimpleNamespace


def _monitor(tmp_path, *, live_trading: bool):
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = live_trading
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._execution_context = SimpleNamespace(
        requested_mode="paper" if not live_trading else "live",
        effective_mode="paper" if not live_trading else "live_ready",
        session_id="sess")
    return monitor


def _open_paper_state(tmp_path, monkeypatch, **fields):
    st = tmp_path / "mts_position_state.json"
    payload = {
        "has_position": True, "near_side": "LONG", "far_side": "SHORT",
        "released_leg": "far", "trade_id": "mts-auto-paper-open",
    }
    payload.update(fields)
    st.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("MTS_STATE_PATH", str(st))


def test_paper_mode_resolves_open_position_from_local_state_not_broker(
        tmp_path, monkeypatch):
    """The exact live incident: broker canonical OK + empty (no paper
    position exists there) must NOT short-circuit to flat in paper mode."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    _open_paper_state(tmp_path, monkeypatch)

    monitor = _monitor(tmp_path, live_trading=False)
    _called = {"n": 0}

    def _snapshot_should_not_be_queried():
        _called["n"] += 1
        return {"fetch_status": {"capture": "OK"}, "positions": [],
                "open_orders": []}

    monitor._capture_post_startup_snapshot = _snapshot_should_not_be_queried

    _has_pos, _near_side, _far_side, _released_leg, _tid, _disk = \
        monitor._resolve_close_all_position()

    assert _has_pos is True
    assert _near_side == "LONG"
    assert _far_side == "SHORT"
    assert _tid == "mts-auto-paper-open"
    # Paper mode must not even call the broker canonical for this decision.
    assert _called["n"] == 0


def test_live_mode_still_treats_empty_canonical_as_authoritative_flat(
        tmp_path, monkeypatch):
    """Regression guard: the anti-ghost-resurrection LIVE invariant from
    test_close_all_canonical_flat.py must be unaffected by this fix."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    _open_paper_state(tmp_path, monkeypatch)  # stale/ghost local state

    monitor = _monitor(tmp_path, live_trading=True)
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "OK"}, "positions": [], "open_orders": []}

    r = monitor._resolve_close_all_position()

    assert r[0] is False
    assert r[1] is None and r[2] is None
