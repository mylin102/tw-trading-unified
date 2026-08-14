from types import SimpleNamespace


def _snapshot(canonical_hash, positions):
    return {
        "fetch_status": {"capture": "OK"},
        "open_orders": [],
        "canonical_input_hash": canonical_hash,
        "account_identity_hash": "account-hash",
        "positions": positions,
    }


def _legs():
    return [
        {"account": "futures", "code": "TMFH6", "quantity": 1,
         "direction": "Action.Sell", "avg_cost": 46411.0},
        {"account": "futures", "code": "TMFI6", "quantity": 1,
         "direction": "Action.Buy", "avg_cost": 46569.0},
    ]


def test_broker_trade_id_ignores_capture_hash_and_cost():
    from strategies.futures.monitor import FuturesMonitor

    first = FuturesMonitor._stable_broker_trade_id("account-hash", _legs())
    changed_cost = [dict(row, avg_cost=float(row["avg_cost"]) + 11)
                    for row in _legs()]
    second = FuturesMonitor._stable_broker_trade_id("account-hash", changed_cost)
    assert first == second
    assert first.startswith("broker-reconciled-")


def test_refresh_uses_stable_broker_identity_across_snapshots(monkeypatch):
    from core.mode_transition import ModeTransitionState
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor._execution_context = SimpleNamespace(
        effective_mode=ModeTransitionState.LIVE_READY.value)
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._live_broker_authority_at = 0.0
    monitor._persist_current_session_canonical = lambda snapshot: None
    snapshots = iter((_snapshot("capture-a", _legs()),
                      _snapshot("capture-b", _legs())))
    monitor._capture_post_startup_snapshot = lambda: next(snapshots)
    strategy = SimpleNamespace(_trade_id="mts-auto-old")

    monitor._refresh_live_broker_authority(strategy)
    first = strategy._trade_id
    monitor._live_broker_authority_at = 0.0
    monitor._refresh_live_broker_authority(strategy)

    assert first == FuturesMonitor._stable_broker_trade_id("account-hash", _legs())
    assert strategy._trade_id == first
    assert first != "mts-auto-old"


def test_broker_observed_position_blocks_entry_even_when_local_strategy_flat():
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._broker_position_observed = True
    monitor._mts_has_open_position_from_fills = lambda: False
    monitor._mts_has_pending_mts_orders = lambda: False
    strategy = SimpleNamespace(_has_position=False)

    assert monitor._mts_block_entry_if_open_position(
        strategy, "SELL_NEAR_BUY_FAR") is True


def test_flat_successful_snapshot_clears_broker_entry_block(monkeypatch):
    from core.mode_transition import ModeTransitionState
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor._execution_context = SimpleNamespace(
        effective_mode=ModeTransitionState.LIVE_READY.value)
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._live_broker_authority_at = 0.0
    monitor._persist_current_session_canonical = lambda snapshot: None
    monitor._capture_post_startup_snapshot = lambda: _snapshot("flat", [])
    strategy = SimpleNamespace(_trade_id=None)

    assert monitor._refresh_live_broker_authority(strategy) is None
    assert monitor._broker_position_observed is False
