"""Regression & contract tests for SUBMITTING + _has_position=True contradiction guard.

2026-11-25 Gemini CLI: Covers T1-T6 for the fix ensuring confirmed positions
(_has_position is True) are never blocked from _manage_position() by a stale
_lifecycle == "SUBMITTING" marker (reachable in paper_active when
_reconstruct_position_from_ledger sets _has_position=True before a try-block
failure leaves _lifecycle at "SUBMITTING").
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from core.strategy_context import MarketData, PositionView, StrategyContext
from strategies.futures.monitor import FuturesMonitor
from strategies.futures.mts_ledger_authority import MtsAuthority
from strategies.plugins.futures.active import tmf_spread
from strategies.plugins.futures.active.tmf_spread import (
    PositionLifecycle,
    PositionPhase,
    ReleaseGroup,
    ReleaseGroupStatus,
    TMFSpread,
    TrailGroup,
    TrailGroupStatus,
)


def _make_bar(near_close=45700.0, far_close=46000.0, atr=10.0, spread_z=0.0, ts=None):
    ts = ts or datetime(2026, 11, 25, 10, 15, 0)
    return {
        "timestamp": ts,
        "near_close": near_close,
        "far_close": far_close,
        "atr": atr,
        "near_bid": near_close - 1.0,
        "near_ask": near_close + 1.0,
        "far_bid": far_close - 1.0,
        "far_ask": far_close + 1.0,
        "near_high": near_close + 50.0,
        "near_low": near_close - 50.0,
        "far_high": far_close + 50.0,
        "far_low": far_close - 50.0,
        "near_tick_age_ms": 10.0,
        "far_tick_age_ms": 10.0,
        "quote_age_ms": 10.0,
        "pair_skew_ms": 0.0,
        "spread_z": spread_z,
        "near_vwap": near_close,
        "far_vwap": far_close,
    }


def _make_strategy(tmp_path, monkeypatch, *, has_position=True, lifecycle="SUBMITTING"):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TRADING_RUNTIME_ID", raising=False)
    monkeypatch.delenv("MTS_STATE_PATH", raising=False)
    monkeypatch.delenv("MTS_BACKTEST", raising=False)

    events = []
    monkeypatch.setattr(
        tmf_spread,
        "_append_event",
        lambda event, **payload: events.append((event, payload)),
    )
    monkeypatch.setattr(tmf_spread, "_write_mts_state", lambda **kwargs: None)
    monkeypatch.setattr(tmf_spread, "_write_mts_telemetry", lambda **kwargs: None)

    config = {
        "ticker": "TMF",
        "params": {
            "entry_z": 3.0,
            "min_atr": 1.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 2.0,
            "release_stop_points": 20,
            "trail_distance_points": 30,
            "confirm_ticks": 1,
            "confirm_ms": 0,
            "max_quote_age_ms": 999999,
            "max_spread_width": 999999,
            "mfe_tighten": {"enabled": False},
            "post_release": {"breakeven_after_atr": 999, "force_lock_after_atr": 999},
        },
    }

    init_bar = _make_bar()
    strategy = TMFSpread()
    strategy.init(
        StrategyContext(
            market=MarketData(last_bar=init_bar, ticker="TMF"),
            position=PositionView(size=0),
            config=config,
        )
    )
    strategy._state_file = str(tmp_path / "mts_position_state.json")
    strategy._has_position = has_position
    strategy._lifecycle = lifecycle
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    strategy._near_entry = 45700.0
    strategy._far_entry = 46000.0
    strategy._near_side = "SHORT"
    strategy._far_side = "LONG"
    strategy._side = "SHORT"
    strategy._trade_id = "mts-submitting-pos-001"
    strategy._entry_ts = datetime(2026, 11, 25, 10, 0, 0)
    strategy._peak = 45700.0
    strategy._nadir = 46000.0
    strategy._release_near_ticks = 0
    strategy._release_far_ticks = 0
    strategy._release_near_start_time = 0.0
    strategy._release_far_start_time = 0.0
    strategy._release_mono = 0.0
    return strategy, config, events


def _ctx(bar, config, size=2):
    return StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=size),
        config=config,
    )


def test_t1a_hydrate_strategy_position_reconciled_exit_only_still_exits(tmp_path, monkeypatch):
    """T1a (reconciled_exit_only real callsite): _hydrate_strategy_position sets
    _has_position=True via setattr without touching _lifecycle, leaving
    _lifecycle=='SUBMITTING'. Subsequent on_bar must reach _manage_position()
    and emit exit/release, never an entry.
    """
    strategy, config, events = _make_strategy(
        tmp_path, monkeypatch, has_position=False, lifecycle="SUBMITTING"
    )
    monitor = object.__new__(FuturesMonitor)
    monitor._exit_only_position = {
        "trade_id": "mts-hydrate-001",
        "reconciliation_id": "rec-001",
        "snapshot_hash": "snap-001",
        "legs": [
            {"code": "TMFJ6", "side": "sell", "quantity": 1, "avg_cost": 45700.0},
            {"code": "TMFK6", "side": "buy", "quantity": 1, "avg_cost": 46000.0},
        ],
    }

    assert monitor._hydrate_strategy_position(strategy) is True
    assert strategy._has_position is True
    assert strategy._lifecycle == "SUBMITTING"

    # Near SHORT entry=45700 -> near_close=45725 is -25pt loss (>= release_stop_points=20)
    bar = _make_bar(near_close=45725.0, far_close=46000.0)
    signal = strategy.on_bar(_ctx(bar, config, size=2))

    assert signal is not None
    assert signal.action not in ("BUY_NEAR_SELL_FAR", "SELL_NEAR_BUY_FAR")
    assert signal.action == "PARTIAL_EXIT"
    assert signal.reason == "TMF_RELEASE_NEAR"
    assert any(name == "SUBMITTING_WITH_POSITION" for name, _ in events)


def test_t1b_ledger_reconstruct_exception_in_paper_active_still_exits(tmp_path, monkeypatch):
    """T1b (paper_active real callsite): _reconstruct_position_from_ledger sets
    _has_position=True before its try block; when infer_lifecycle_from_legacy_state
    raises and except swallows it, _lifecycle remains 'SUBMITTING'. Subsequent
    on_bar must reach _manage_position() and emit exit/release, never an entry.
    """
    strategy, config, events = _make_strategy(
        tmp_path, monkeypatch, has_position=False, lifecycle="SUBMITTING"
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated lifecycle inference failure")

    monkeypatch.setattr(tmf_spread, "infer_lifecycle_from_legacy_state", _boom)

    monitor = object.__new__(FuturesMonitor)
    monitor.ticker = "TMF"
    auth = SimpleNamespace(
        status=MtsAuthority.OPEN,
        trade_id="mts-reconstruct-001",
        near_side="SHORT",
        far_side="LONG",
        near_qty=-1,
        far_qty=1,
        near_entry=45700.0,
        far_entry=46000.0,
    )
    monitor._reconstruct_position_from_ledger(strategy, auth)

    # Confirm the exact contradiction state produced in paper_active
    assert strategy._has_position is True
    assert strategy._lifecycle == "SUBMITTING"

    bar = _make_bar(near_close=45725.0, far_close=46000.0)
    signal = strategy.on_bar(_ctx(bar, config, size=2))

    assert signal is not None
    assert signal.action == "PARTIAL_EXIT"
    assert signal.reason == "TMF_RELEASE_NEAR"
    assert any(name == "SUBMITTING_WITH_POSITION" for name, _ in events)


def test_t2_submitting_without_position_stays_mts_busy_no_entry(tmp_path, monkeypatch):
    """T2: _has_position=False + _lifecycle='SUBMITTING' stays MTS_BUSY and never enters."""
    strategy, config, events = _make_strategy(
        tmp_path, monkeypatch, has_position=False, lifecycle="SUBMITTING"
    )
    bar = _make_bar(near_close=39534.0, far_close=39698.0, atr=20.0, spread_z=-4.0)

    signal = strategy.on_bar(_ctx(bar, config, size=0))

    assert signal is None
    assert strategy.last_eval.skip_reason == "MTS_BUSY"
    assert strategy._lifecycle == "SUBMITTING"
    assert not any(name == "SUBMITTING_WITH_POSITION" for name, _ in events)
    assert any(
        name == "ENTRY_GATE_SKIP" and payload.get("skip_reason") == "MTS_BUSY"
        for name, payload in events
    )


def test_t3_release_near_with_live_broker_order_stays_pending(tmp_path, monkeypatch):
    """T3 (S2): RELEASE_NEAR >60s with live broker order keeps pending and does not resend."""
    strategy, config, events = _make_strategy(
        tmp_path, monkeypatch, has_position=True, lifecycle="RELEASE_NEAR"
    )
    strategy._release_mono = 100.0
    strategy._broker_truth_flat = False
    monkeypatch.setattr(tmf_spread.time, "monotonic", lambda: 200.0)

    bar = _make_bar(near_close=45725.0, far_close=46000.0)
    signal = strategy.on_bar(_ctx(bar, config, size=2))

    assert signal is None
    assert strategy._lifecycle == "RELEASE_NEAR"
    assert strategy.last_eval.skip_reason == "MTS_RELEASE_PENDING_BROKER_LIVE"
    assert not any(name == "SUBMITTING_WITH_POSITION" for name, _ in events)


def test_t4_exiting_with_position_blocks_duplicate_exit(tmp_path, monkeypatch):
    """T4 (S2): _lifecycle='EXITING' with open position must return None (MTS_BUSY) and never resend."""
    strategy, config, events = _make_strategy(
        tmp_path, monkeypatch, has_position=True, lifecycle="EXITING"
    )
    bar = _make_bar(near_close=45680.0, far_close=46000.0)

    signal = strategy.on_bar(_ctx(bar, config, size=2))

    assert signal is None
    assert strategy._lifecycle == "EXITING"
    assert strategy.last_eval.skip_reason == "MTS_BUSY"
    assert not any(name == "SUBMITTING_WITH_POSITION" for name, _ in events)


def test_t5_unknown_has_position_none_with_submitting_fails_closed(tmp_path, monkeypatch):
    """T5 (S6): _has_position=None + _lifecycle='SUBMITTING' fails closed and never manages."""
    strategy, config, events = _make_strategy(
        tmp_path, monkeypatch, has_position=None, lifecycle="SUBMITTING"
    )
    bar = _make_bar(near_close=45680.0, far_close=46000.0)

    signal = strategy.on_bar(_ctx(bar, config, size=0))

    assert signal is None
    assert strategy._has_position is None
    assert strategy._lifecycle == "SUBMITTING"
    assert strategy.last_eval.skip_reason == "MTS_BUSY"
    assert not any(name == "SUBMITTING_WITH_POSITION" for name, _ in events)


def test_t6_contradiction_telemetry_fields_and_30s_rate_limit(tmp_path, monkeypatch):
    """T6 (S7): Contradiction emits typed event with required fields and 30s rate-limit."""
    strategy, config, events = _make_strategy(
        tmp_path, monkeypatch, has_position=True, lifecycle="SUBMITTING"
    )
    clock = {"now": 100.0}
    monkeypatch.setattr(tmf_spread.time, "monotonic", lambda: clock["now"])

    # Flat PnL bar so _manage_position does not exit and lifecycle stays SUBMITTING
    base_ts = datetime(2026, 11, 25, 10, 15, 0)
    for idx, mono_now in enumerate((100.0, 110.0, 129.0)):
        clock["now"] = mono_now
        bar = _make_bar(
            near_close=45700.0,
            far_close=46000.0,
            ts=base_ts + timedelta(seconds=idx * 10),
        )
        strategy.on_bar(_ctx(bar, config, size=2))

    contradiction_events = [
        payload for name, payload in events if name == "SUBMITTING_WITH_POSITION"
    ]
    assert len(contradiction_events) == 1
    payload = contradiction_events[0]
    assert payload["lifecycle"] == "SUBMITTING"
    assert payload["has_position"] is True
    assert payload["trade_id"] == "mts-submitting-pos-001"
    assert payload["near_entry"] == 45700.0
    assert payload["far_entry"] == 46000.0

    # After >= 30s cooldown, a second event is emitted
    clock["now"] = 130.5
    bar_after = _make_bar(
        near_close=45700.0,
        far_close=46000.0,
        ts=base_ts + timedelta(seconds=31),
    )
    strategy.on_bar(_ctx(bar_after, config, size=2))

    contradiction_events = [
        payload for name, payload in events if name == "SUBMITTING_WITH_POSITION"
    ]
    assert len(contradiction_events) == 2
