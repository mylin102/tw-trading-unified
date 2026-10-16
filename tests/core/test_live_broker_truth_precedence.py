"""LIVE broker-truth precedence over local MTS recovery artifacts."""

from types import SimpleNamespace


def _live_monitor():
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._execution_context = SimpleNamespace(requested_mode="live")
    monitor._live_broker_authority_at = 0.0
    monitor._live_broker_authority = None
    monitor._broker_position_observed = True
    monitor._live_broker_flat_proven = False
    monitor._broker_authority_degraded = False
    monitor._persist_current_session_canonical = lambda snapshot: None
    return monitor


def _flat_snapshot():
    return {
        "fetch_status": {"capture": "OK"},
        "open_orders": [],
        "positions": [],
        "account_identity_hash": "account-hash",
    }


def test_successful_flat_broker_snapshot_beats_fills_ghost_even_if_local_reconcile_fails(
    monkeypatch, tmp_path,
):
    """A local duplicate-fill error cannot hide a successful broker-flat proof."""
    from strategies.futures.monitor import FuturesMonitor
    from strategies.futures.mts_ledger_authority import MtsAuthority
    import strategies.futures.monitor as monitor_module

    monitor = _live_monitor()
    monitor._capture_post_startup_snapshot = _flat_snapshot
    monitor._reconcile_local_orders_from_snapshot = lambda snapshot: (_ for _ in ()).throw(
        ValueError("Fill qty 1 exceeds remaining 0 for ORD-duplicate"))
    strategy = SimpleNamespace(
        _has_position=True, _trade_id="ghost", _broker_truth_flat=False)

    authority = monitor._refresh_live_broker_authority(strategy)

    assert authority.status is MtsAuthority.FLAT
    assert strategy._broker_truth_flat is True
    assert monitor._live_broker_flat_proven is True
    assert monitor._broker_authority_degraded is False

    state_path = tmp_path / "mts_position_state.json"
    state_path.write_text('{"has_position": true, "lifecycle": {"phase": "SPREAD"}}')
    monkeypatch.setattr(monitor_module, "_mts_position_state_path", lambda: state_path)
    monitor._mts_has_open_position_from_fills = lambda: True
    monitor._mts_has_pending_mts_orders = lambda: False

    # The proven-flat broker state supersedes stale state/fills artifacts;
    # the strategy is reset by the pre-signal gate before an order can exist.
    assert monitor._mts_block_entry_if_open_position(
        strategy, "SELL_NEAR_BUY_FAR") is False


def test_failed_live_snapshot_revokes_flat_proof_and_preserves_known_open_state():
    """Unknown broker truth is degraded/blocked, never treated as a synthetic exit."""
    monitor = _live_monitor()
    monitor._live_broker_flat_proven = True
    monitor._broker_authority_degraded = False
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "ERROR"},
        "open_orders": [],
        "positions": [],
    }
    monitor._reconcile_local_orders_from_snapshot = lambda snapshot: (_ for _ in ()).throw(
        AssertionError("failed capture must not reconcile local fills"))
    strategy = SimpleNamespace(
        _has_position=True, _trade_id="known-open", _broker_truth_flat=False)

    assert monitor._refresh_live_broker_authority(strategy) is None
    assert monitor._live_broker_flat_proven is False
    assert monitor._broker_authority_degraded is True
    assert strategy._has_position is True
    assert strategy._trade_id == "known-open"
    assert strategy._broker_truth_flat is False
