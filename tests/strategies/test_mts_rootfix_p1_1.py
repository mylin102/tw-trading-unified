from types import SimpleNamespace


def _monitor(snapshot):
    from strategies.futures.monitor import FuturesMonitor
    from core.mode_transition import ExecutionContext, ModeTransitionState

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor._execution_context = ExecutionContext(
        requested_mode="live", effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
    )
    monitor._live_broker_authority_at = 0.0
    monitor._live_broker_authority = None
    monitor.contract = SimpleNamespace(code="TMFJ6")
    monitor.far_contract = SimpleNamespace(code="TMFK6")
    monitor._capture_post_startup_snapshot = lambda: snapshot
    monitor._reconcile_local_orders_from_snapshot = lambda _snapshot: None
    monitor._run_broker_reconciliation_detector = lambda _snapshot: None
    monitor._persist_current_session_canonical = lambda _snapshot: None
    return monitor


def test_p1_1_successful_flat_snapshot_sets_truth_flag():
    from strategies.futures.mts_ledger_authority import MtsAuthority
    monitor = _monitor({
        "positions": [], "open_orders": [],
        "fetch_status": {"capture": "OK"},
    })
    strategy = SimpleNamespace(_has_position=False)
    authority = monitor._refresh_live_broker_authority(strategy)
    assert authority is not None
    assert authority.status == MtsAuthority.FLAT
    assert strategy._broker_truth_flat is True


def test_p1_1_query_failure_never_sets_flat_truth():
    monitor = _monitor({
        "positions": [], "open_orders": [],
        "fetch_status": {"capture": "FAIL"},
    })
    strategy = SimpleNamespace(_has_position=False)
    monitor._refresh_live_broker_authority(strategy)
    assert strategy._broker_truth_flat is False


def test_p1_1_flat_truth_blocks_fills_resurrection(tmp_path, monkeypatch):
    from strategies.plugins.futures.active import tmf_spread as mod
    path = tmp_path / "fills.jsonl"
    path.write_text('{"trade_id":"old","fill_type":"ENTRY"}\n')
    monkeypatch.setattr(mod, "_MTS_FILL_LOG", str(path))
    strategy = mod.TMFSpread.__new__(mod.TMFSpread)
    strategy._broker_truth_flat = True
    assert strategy._restore_from_fills_log() is False


def test_mutation_p1_1_requires_explicit_flat_assignment():
    from pathlib import Path
    source = Path("strategies/futures/monitor.py").read_text()
    assert "strategy._broker_truth_flat = True" in source
