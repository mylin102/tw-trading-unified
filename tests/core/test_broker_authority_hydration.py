"""P0-A: broker-truth position hydration must deliver qty/sides/entry_time/trade_id
to the strategy BEFORE release / Policy-J evaluation, and
RELEASE_EVAL_SKIP_NO_LOCAL_POSITION must not fire when broker evidence is valid.

Bounded contract (postmortem plan, 2026-08-17):
- 2-leg canonical snapshot  -> OPEN authority + full strategy hydration
- remaining-leg snapshot   -> SINGLE_LEG authority + real-leg hydration, NO
  synthetic leg (missing leg qty stays 0)
- entry_time is hydrated from broker deal/trade timestamps so Policy J's
  guard clock is never ENTRY_TIME_MISSING for a broker-observed position
- restart: same snapshot -> same stable trade_id + same entry epoch
- invalid/capture-failed snapshot -> fail-closed: no hydration, no flat proof,
  entry stays blocked
- the skip event is emitted ONLY when the strategy is genuinely unresolved —
  never during the pre-hydration window of a valid snapshot
"""
from types import SimpleNamespace
from datetime import datetime


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
    monitor._release_eval_skip_last_emit = 0.0
    return monitor


def _snap(positions, open_orders=(), broker_trades=(), capture="OK"):
    return {
        "fetch_status": {"capture": capture},
        "account_identity_hash": "hash-1",
        "positions": list(positions),
        "open_orders": list(open_orders),
        "broker_trades": list(broker_trades),
    }


def _two_leg_snap():
    """Canonical: TMFH6 Sell 1 + TMFI6 Buy 1, both deals timestamped."""
    return _snap(
        positions=[
            {"account": "futures", "code": "TMFH6", "quantity": 1,
             "direction": "Action.Sell", "avg_cost": 45879.0},
            {"account": "futures", "code": "TMFI6", "quantity": 1,
             "direction": "Action.Buy", "avg_cost": 46033.0},
        ],
        broker_trades=[
            {"id": "BRK-NEAR", "code": "TMFH6", "status": "Filled",
             "direction": "sell", "price": 45879.0, "quantity": 1,
             "ts": "2026-08-17T09:01:00+08:00"},
            {"id": "BRK-FAR", "code": "TMFI6", "status": "Filled",
             "direction": "buy", "price": 46033.0, "quantity": 1,
             "ts": "2026-08-17T09:01:02+08:00"},
        ],
    )


def _strategy(**over):
    base = dict(_has_position=False, _trade_id="old", _near_qty=0, _far_qty=0,
                _near_side=None, _far_side=None, _near_entry=0.0, _far_entry=0.0,
                _released_leg=None, _lifecycle="FLAT",
                _entry_guard_start_ms=None, _entry_ts_ms=None, _entry_ts=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_two_leg_hydration_sets_entry_time_trade_id_and_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    from strategies.futures.mts_ledger_authority import MtsAuthority

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _two_leg_snap()
    strategy = _strategy()
    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is not None
    assert auth.status is MtsAuthority.OPEN
    assert auth.near_qty == -1 and auth.far_qty == 1
    assert strategy._near_qty == 1 and strategy._far_qty == 1
    assert strategy._near_side == "SHORT" and strategy._far_side == "LONG"
    # entry costs are hydrated as floats the evaluator can use
    assert strategy._near_entry == 45879.0 and strategy._far_entry == 46033.0
    assert strategy._has_position is True
    # stable broker-derived trade identity
    assert strategy._trade_id.startswith("broker-reconciled-")
    # entry_time hydration: Policy J guard clock must be anchored to the
    # broker deal timestamp (earliest of the two legs)
    _expected_ms = datetime.fromisoformat(
        "2026-08-17T09:01:00+08:00").timestamp() * 1000.0
    assert strategy._entry_guard_start_ms is not None
    assert abs(strategy._entry_guard_start_ms - _expected_ms) < 2000.0
    assert strategy._entry_ts_ms == strategy._entry_guard_start_ms
    assert strategy._entry_ts is not None
    # entry stays blocked while the broker position exists
    assert monitor._broker_position_observed is True
    assert monitor._live_broker_flat_proven is False


def test_single_leg_hydration_sets_entry_time_no_synthetic_leg(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    from strategies.futures.mts_ledger_authority import MtsAuthority

    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap(
        positions=[
            {"account": "futures", "code": "TMFI6", "quantity": 1,
             "direction": "Action.Buy", "avg_cost": 46033.0},
        ],
        broker_trades=[
            {"id": "BRK-FAR", "code": "TMFI6", "status": "Filled",
             "direction": "buy", "price": 46033.0, "quantity": 1,
             "ts": "2026-08-17T23:35:00+08:00"},
        ],
    )
    strategy = _strategy()
    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is not None
    assert auth.status is MtsAuthority.SINGLE_LEG
    assert strategy._far_qty == 1 and strategy._far_side == "LONG"
    assert strategy._near_qty == 0 and strategy._near_side is None  # no synthetic
    assert strategy._has_position is True
    _expected_ms = datetime.fromisoformat(
        "2026-08-17T23:35:00+08:00").timestamp() * 1000.0
    assert strategy._entry_guard_start_ms is not None
    assert abs(strategy._entry_guard_start_ms - _expected_ms) < 2000.0
    assert strategy._entry_ts is not None
    assert monitor._broker_position_observed is True


def test_restart_keeps_stable_trade_id_and_entry_epoch(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    first = _monitor(tmp_path)
    first._capture_post_startup_snapshot = lambda: _two_leg_snap()
    s1 = _strategy()
    first._refresh_live_broker_authority(s1)

    # simulated restart: a fresh process sees the same broker snapshot
    second = _monitor(tmp_path)
    second._capture_post_startup_snapshot = lambda: _two_leg_snap()
    s2 = _strategy()
    second._refresh_live_broker_authority(s2)

    assert s1._trade_id == s2._trade_id
    assert s1._entry_guard_start_ms is not None
    assert s1._entry_guard_start_ms == s2._entry_guard_start_ms
    assert s2._near_qty == 1 and s2._far_qty == 1


def test_entry_time_binds_to_current_position_generation(tmp_path, monkeypatch):
    """An OLD unrelated deal for the same code must not anchor the current
    position's entry clock — the timestamp is bound to the current position
    generation (direction + qty + avg_cost price basis)."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap(
        positions=[
            {"account": "futures", "code": "TMFI6", "quantity": 1,
             "direction": "Action.Buy", "avg_cost": 46033.0},
        ],
        broker_trades=[
            # old generation: same code + direction, different price basis
            {"id": "OLD-DEAL", "code": "TMFI6", "status": "Filled",
             "direction": "buy", "price": 45200.0, "quantity": 1,
             "ts": "2026-08-10T09:01:00+08:00"},
            # current entry fill: price == avg_cost
            {"id": "CUR-DEAL", "code": "TMFI6", "status": "Filled",
             "direction": "buy", "price": 46033.0, "quantity": 1,
             "ts": "2026-08-17T23:35:00+08:00"},
        ],
    )
    strategy = _strategy()
    monitor._refresh_live_broker_authority(strategy)

    _expected = datetime.fromisoformat(
        "2026-08-17T23:35:00+08:00").timestamp() * 1000.0
    assert strategy._entry_guard_start_ms is not None
    assert abs(strategy._entry_guard_start_ms - _expected) < 2000.0


def test_entry_time_ambiguous_generation_fails_closed(tmp_path, monkeypatch):
    """Without a cost basis the resolver cannot tell an old generation from
    the current one -> no anchor (fail-closed), never the old timestamp."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap(
        positions=[
            {"account": "futures", "code": "TMFH6", "quantity": 1,
             "direction": "Action.Sell"},
            {"account": "futures", "code": "TMFI6", "quantity": 1,
             "direction": "Action.Buy"},
        ],
        broker_trades=[
            {"id": "OLD-DEAL", "code": "TMFI6", "status": "Filled",
             "direction": "buy", "price": 45200.0, "quantity": 1,
             "ts": "2026-08-10T09:01:00+08:00"},
            {"id": "CUR-DEAL", "code": "TMFI6", "status": "Filled",
             "direction": "buy", "price": 46033.0, "quantity": 1,
             "ts": "2026-08-17T23:35:00+08:00"},
        ],
    )
    strategy = _strategy()
    monitor._refresh_live_broker_authority(strategy)

    # position still hydrated (2-leg), but no trustworthy entry clock
    assert strategy._has_position is True
    assert strategy._entry_guard_start_ms is None
    assert strategy._entry_ts_ms is None
    assert strategy._entry_ts is None


def test_valid_hydration_without_anchor_clears_stale_entry_clock(
        tmp_path, monkeypatch):
    """A valid broker position with NO trustworthy anchor must explicitly
    clear any stale prior entry clock — a previous trade's clock must never
    survive into the new position (would bypass ENTRY_TIME_MISSING)."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _two_leg_snap_without_deals()
    strategy = _strategy(
        _entry_guard_start_ms=1111111111111.0,   # stale prior-trade clock
        _entry_ts_ms=1111111111111.0,
        _entry_ts=datetime(2025, 1, 1))
    monitor._refresh_live_broker_authority(strategy)

    assert strategy._has_position is True        # broker position valid
    assert strategy._entry_guard_start_ms is None  # stale clock cleared
    assert strategy._entry_ts_ms is None
    assert strategy._entry_ts is None


def _two_leg_snap_without_deals():
    """Two-leg snapshot with NO broker_trades / fills / state -> no anchor."""
    return _snap(
        positions=[
            {"account": "futures", "code": "TMFH6", "quantity": 1,
             "direction": "Action.Sell", "avg_cost": 45879.0},
            {"account": "futures", "code": "TMFI6", "quantity": 1,
             "direction": "Action.Buy", "avg_cost": 46033.0},
        ],
        broker_trades=[],
    )


def test_invalid_snapshot_fail_closed_no_hydration(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap(
        positions=[{"account": "futures", "code": "TMFH6", "quantity": 1,
                    "direction": "Action.Sell", "avg_cost": 45879.0}],
        capture="FAIL",
    )
    strategy = _strategy()
    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is None
    assert strategy._has_position is False        # untouched
    assert strategy._entry_guard_start_ms is None  # no fabricated entry time
    assert monitor._broker_authority_degraded is True
    assert monitor._live_broker_flat_proven is False
    assert monitor._broker_position_observed is True  # entry fail-closed


def test_skip_event_not_emitted_during_valid_snapshot_hydration(
        tmp_path, monkeypatch):
    """A valid 2-leg snapshot must hydrate the strategy WITHOUT a
    RELEASE_EVAL_SKIP_NO_LOCAL_POSITION event, even when the strategy starts
    pre-hydration (local qty 0) inside the refresh call."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _two_leg_snap()
    events = []
    monitor._append_mts_event = (
        lambda *a, **kw: events.append({"event": a[0], **kw}))
    strategy = _strategy()  # pre-hydration local qty = 0
    monitor._mts_strategy = strategy

    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is not None
    assert strategy._near_qty == 1  # hydration happened
    skip = [e for e in events
            if e.get("event") == "RELEASE_EVAL_SKIP_NO_LOCAL_POSITION"]
    assert skip == [], f"skip event fired despite valid broker evidence: {skip}"


def test_skip_event_fires_when_broker_legs_unresolvable(tmp_path, monkeypatch):
    """When broker legs exist but hydration fails (ambiguous direction), the
    skip event is the CORRECT telemetry and must still fire."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor = _monitor(tmp_path)
    monitor._capture_post_startup_snapshot = lambda: _snap(
        positions=[
            {"account": "futures", "code": "TMFH6", "quantity": 1,
             "direction": "SOMETHING_ELSE", "avg_cost": 45879.0},
        ],
    )
    events = []
    monitor._append_mts_event = (
        lambda *a, **kw: events.append({"event": a[0], **kw}))
    strategy = _strategy()
    monitor._mts_strategy = strategy

    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is None
    assert monitor._broker_authority_degraded is True
    skip = [e for e in events
            if e.get("event") == "RELEASE_EVAL_SKIP_NO_LOCAL_POSITION"]
    assert len(skip) == 1
    assert "TMFH6" in skip[0]["broker_legs"][0]


def test_policy_j_guard_clock_accepts_hydrated_entry_time(monkeypatch):
    """After hydration the strategy's guard clock is anchored: Policy J must
    not suppress with ENTRY_TIME_MISSING when entry_ts_ms comes from the
    hydrated _entry_guard_start_ms."""
    from strategies.plugins.futures.active import tmf_spread
    from strategies.plugins.futures.active.tmf_spread import TMFSpread

    events = []
    monkeypatch.setattr(tmf_spread, "_append_event",
                        lambda *a, **kw: events.append({"event": a[0], **kw}))

    s = TMFSpread.__new__(TMFSpread)
    s._near_entry = 45879.0
    s._far_entry = 46033.0
    s._trade_id = "broker-reconciled-abc"
    s._point_value = 10.0
    s._estimated_cost = 92.0
    s._pj_last_suppress_ms = 0.0
    s._entry_guard_start_ms = datetime.fromisoformat(
        "2026-08-17T09:01:00+08:00").timestamp() * 1000.0
    now_ms = s._entry_guard_start_ms + 60000.0  # 60s after entry

    ok = s._update_policy_j_peak(
        current_pnl_pts=10.0, near_mark=45900.0, far_mark=46050.0,
        now_ms=now_ms, mark_age_ms=0.0, pair_skew_ms=0.0,
        entry_ts_ms=float(s._entry_guard_start_ms), phase="SPREAD")

    suppressed = [e for e in events
                  if e.get("event") == "POLICY_J_TRIGGER_SUPPRESSED"
                  and e.get("reason") == "ENTRY_TIME_MISSING"]
    assert suppressed == [], f"hydrated entry time was rejected: {suppressed}"
    assert ok is not None
