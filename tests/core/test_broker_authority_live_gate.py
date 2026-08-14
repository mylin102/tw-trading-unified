"""Broker authority must be LIVE from the FIRST tick: the refresh guard keyed
on live_trading (key absent in futures_live.yaml) + effective_mode==live_ready
(not set until the certificate transition) returns None at startup, which
falls back to the stale fills-ledger authority and resurrects the ghost via
_reconstruct_position_from_ledger (reason=authority_rebuild observed).
Gate on the ctx requested_mode (live intent, set at ctx creation) instead.
"""
from types import SimpleNamespace

from strategies.futures.monitor import FuturesMonitor
from tests.core.test_fills_recovery_live_gate import _AutoMonitor


def test_broker_authority_runs_from_first_tick_in_live(tmp_path, monkeypatch):
    mon = _AutoMonitor.__new__(_AutoMonitor)
    mon.live_trading = False  # futures_live.yaml has NO live_trading key!
    mon._execution_context = SimpleNamespace(
        requested_mode="live", effective_mode="live_preflight")  # startup!
    mon._live_broker_authority_at = 0.0
    mon._live_broker_authority = None
    mon.contract = SimpleNamespace(code="TMFH6")
    mon.far_contract = SimpleNamespace(code="TMFI6")

    captured = []
    from strategies.futures.mts_ledger_authority import MtsAuthority

    def _fake_capture():
        captured.append(1)
        return {"fetch_status": {"capture": "OK"},
                "open_orders": [], "positions": [],
                "broker_trades": []}
    mon._capture_post_startup_snapshot = _fake_capture
    mon._reconcile_local_orders_from_snapshot = lambda snap: None
    mon._persist_current_session_canonical = lambda snap: None

    strat = SimpleNamespace(_has_position=False)
    auth = mon._refresh_live_broker_authority(strat)
    # LIVE + pre-transition: the broker query must run (no ledger fallback).
    assert captured == [1]
    assert auth is not None
    assert auth.status == MtsAuthority.FLAT
