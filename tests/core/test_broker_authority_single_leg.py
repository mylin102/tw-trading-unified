"""RED: SINGLE_LEG broker authority.

Current broker canonical: TMFI6 Buy 1 @46033, TMFH6 absent,
open_orders empty, runtime quarantined BROKER_NOT_FLAT.  The strategy
sees no local position (local qty 0) so release/trail evaluation is
skipped while the broker still holds the remaining leg.

Bounded contract (codex):
- hydrate the remaining leg for trailing/release ONLY
- block entry/manual/generic (position observed)
- NO synthetic leg, NO PnL
- ambiguity / missing fields / session / capture failure -> quarantine
- two-leg and Paper behavior preserved
"""

from types import SimpleNamespace


def _monitor(tmp_path):
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._execution_context = SimpleNamespace(
        requested_mode="live", effective_mode="live_ready", session_id="sess")
    monitor._live_broker_authority_at = 0.0
    monitor._broker_position_observed = False
    monitor._live_broker_flat_proven = False
    monitor._broker_authority_degraded = False
    return monitor


def _snap(positions, open_orders=(), capture="OK"):
    return {
        "fetch_status": {"capture": capture},
        "account_identity_hash": "hash-1",
        "positions": positions,
        "open_orders": list(open_orders),
    }


def test_single_leg_canonical_hydrates_remaining_leg(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    from strategies.futures.mts_ledger_authority import MtsAuthority

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap([
        {"account": "futures", "code": "TMFI6", "quantity": 1,
         "direction": "Action.Buy", "avg_cost": 46033.0},
    ])
    strategy = SimpleNamespace(_has_position=False, _trade_id="old")
    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is not None
    assert auth.status is MtsAuthority.SINGLE_LEG
    assert auth.far_qty == 1 and auth.far_side == "LONG"
    assert auth.near_qty == 0 and auth.near_side is None  # no synthetic leg
    assert strategy._has_position is True
    assert strategy._far_qty == 1 and strategy._far_side == "LONG"
    assert strategy._near_qty == 0
    # entry/manual/generic stays blocked
    assert monitor._broker_position_observed is True
    assert monitor._broker_authority_degraded is False


def test_single_leg_near_leg_hydrates_near(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    from strategies.futures.mts_ledger_authority import MtsAuthority

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap([
        {"account": "futures", "code": "TMFH6", "quantity": 1,
         "direction": "Action.Sell", "avg_cost": 45879.0},
    ])
    strategy = SimpleNamespace(_has_position=False, _trade_id="old")
    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is not None
    assert auth.status is MtsAuthority.SINGLE_LEG
    assert auth.near_qty == -1 and auth.near_side == "SHORT"
    assert auth.far_qty == 0 and auth.far_side is None


def test_single_leg_ambiguous_direction_quarantines(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap([
        {"account": "futures", "code": "TMFI6", "quantity": 1,
         "direction": "SOMETHING_ELSE", "avg_cost": 46033.0},
    ])
    strategy = SimpleNamespace(_has_position=False, _trade_id="old")
    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is None
    assert monitor._broker_authority_degraded is True


def test_single_leg_unknown_code_quarantines(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap([
        {"account": "futures", "code": "TMFJ6", "quantity": 1,
         "direction": "Action.Buy", "avg_cost": 46033.0},
    ])
    strategy = SimpleNamespace(_has_position=False, _trade_id="old")
    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is None
    assert monitor._broker_authority_degraded is True
