"""2026-11-25 Gemini CLI: Regression tests for clearing speculative SUBMITTING marker
on local MTS combo entry rejection (ENTRY_BBO_MISSING / ENTRY_BBO_INVALID /
COMBO_PRICE_NONFINITE / COMBO_LIFECYCLE_CREATE_FAILED).

Covers:
- T1: ENTRY_BBO_MISSING clears strategy._lifecycle -> "FLAT" so the next valid bar
  through TMFSpread.on_bar() can emit an entry Signal (no ENTRY_ALREADY_SUBMITTED deadlock).
- T2: ENTRY_BBO_INVALID and COMBO_PRICE_NONFINITE also clear speculative entry markers.
- T3: Existing COMBO_LIFECYCLE_CREATE_FAILED cleanup behavior remains unchanged.
- T4: Real open position (_has_position is True) is NEVER cleared by entry rejection.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from core.strategy_context import MarketData, PositionView, StrategyContext
from strategies.futures.monitor import FuturesMonitor
from strategies.plugins.futures.active import tmf_spread
from strategies.plugins.futures.active.tmf_spread import RecoveryState, TMFSpread


class _OrderManager:
    def __init__(self, *, fail_create: bool = False):
        self.created = []
        self.fail_create = fail_create

    def create_order(self, **kwargs):
        if self.fail_create:
            raise RuntimeError("simulated create_order failure")
        order = SimpleNamespace(
            order_id="LOCAL-1",
            intent_id="INTENT-1",
            status="PENDING",
            broker_order_id=None,
            exchange_order_id=None,
            seqno=None,
            ordno=None,
            reject_reason=None,
            **kwargs,
        )
        self.created.append(order)
        return order


def _make_monitor(*, fail_create: bool = False) -> FuturesMonitor:
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = False
    monitor.dry_run = False
    monitor.paper_fill_sim = None
    monitor.api = None
    monitor.cfg = {"mts": {"params": {}}}
    monitor.order_mgr = _OrderManager(fail_create=fail_create)
    monitor._pending_lifecycle_orders = {}
    monitor._mts_pending_fills = {}
    monitor.events = []
    monitor._append_mts_event = (
        lambda event, **payload: monitor.events.append((event, payload))
    )
    monitor._submit_via_gateway = lambda order: True
    monitor._reconcile_pending_mts_combos = (
        lambda **kwargs: {"status": "BROKER_CONFIRMED"}
    )
    monitor._capture_mts_position_baseline = lambda: []
    return monitor


def _make_strategy(monkeypatch) -> tuple[TMFSpread, StrategyContext]:
    import core.channel_safety as cs

    monkeypatch.setattr(tmf_spread, "_append_event", lambda *a, **kw: None)
    monkeypatch.setattr(tmf_spread, "_write_mts_state", lambda **kw: None)
    monkeypatch.setattr(tmf_spread, "_write_mts_telemetry", lambda **kw: None)
    monkeypatch.setattr(tmf_spread, "_maintenance_entry_lock_active", lambda: False)
    monkeypatch.setattr(
        cs,
        "get_safety_state",
        lambda: SimpleNamespace(entry_allowed=lambda ticker: True, entry_blocked_reason=""),
    )

    s = TMFSpread()
    cfg = {
        "ticker": "TMF",
        "params": {
            "entry_z": 2.5,
            "min_atr": 10.0,
            "release_stop_points": 20.0,
            "trail_distance_points": 20.0,
        },
    }
    ctx = StrategyContext(
        position=PositionView(size=0),
        market=MarketData(ticker="TMF", last_bar={}),
        config=cfg,
    )
    s.init(ctx)
    s._has_position = False
    s._mts_recovery_state = RecoveryState.FLAT_CONFIRMED
    s._lifecycle = "FLAT"
    return s, ctx


def _submit_entry(monitor, strategy, *, near_price=40324.0, far_price=40464.0, **bar_overrides):
    bar = {
        "spread": near_price - far_price,
        "spread_z": 4.72,
        "bbo_near_bid": 40323.0,
        "bbo_near_ask": 40325.0,
        "bbo_far_bid": 40463.0,
        "bbo_far_ask": 40465.0,
        "bbo_near_age_ms": 20.0,
        "bbo_far_age_ms": 30.0,
    }
    bar.update(bar_overrides)
    monitor._submit_mts_combo_entry(
        trade_id="mts-auto-105844-260",
        action="SELL_NEAR_BUY_FAR",
        reason="TMF_SPREAD_WIDE",
        near_code="TMFL6",
        far_code="TMFA7",
        near_side="sell",
        far_side="buy",
        near_price=near_price,
        far_price=far_price,
        bar_dict=bar,
        ts=datetime(2026, 11, 24, 10, 58, 44),
        snapshot={},
        strategy=strategy,
        entry_offset_ticks=2,
        tick_size=1.0,
    )


def test_t1_bbo_missing_clears_submitting_so_next_bar_can_enter(monkeypatch):
    """T1: ENTRY_BBO_MISSING must clear strategy._lifecycle back to FLAT so the
    next valid bar through TMFSpread.on_bar() is not permanently blocked by
    ENTRY_ALREADY_SUBMITTED."""
    strategy, base_ctx = _make_strategy(monkeypatch)
    monitor = _make_monitor()

    # Bar 1: strategy emits entry Signal -> speculatively sets _lifecycle = "SUBMITTING"
    bar1 = {
        "timestamp": datetime(2026, 11, 24, 10, 58, 44),
        "near_close": 40324.0,
        "far_close": 40464.0,
        "spread": -140.0,
        "spread_z": 4.72,
        "atr": 62.0,
    }
    sig1 = strategy.on_bar(
        StrategyContext(
            position=PositionView(size=0),
            market=MarketData(ticker="TMF", last_bar=bar1),
            config=base_ctx.config,
        )
    )
    assert sig1 is not None and sig1.action == "SELL_NEAR_BUY_FAR"
    assert strategy._lifecycle == "SUBMITTING"

    # Monitor rejects locally due to missing BBO (reproducing 2026-11-24 10:58:44)
    _submit_entry(
        monitor,
        strategy,
        bbo_near_bid=None,
        bbo_near_ask=None,
        bbo_far_bid=None,
        bbo_far_ask=None,
    )
    assert any(
        ev == "ORDER_REJECTED_LOCAL" and p.get("reason") == "ENTRY_BBO_MISSING"
        for ev, p in monitor.events
    )
    assert strategy._lifecycle == "FLAT"
    assert strategy._has_position is False
    assert strategy._entry_ts is None
    assert strategy._entry_time_monotonic == 0.0
    assert strategy._entry_guard_start_ms == 0.0
    assert strategy._entry_ts_ms == 0.0

    # Bar 2: next valid bar must produce an entry Signal (not blocked by ENTRY_ALREADY_SUBMITTED)
    bar2 = {
        "timestamp": datetime(2026, 11, 24, 15, 5, 0),
        "near_close": 38500.0,
        "far_close": 38620.0,
        "spread": -120.0,
        "spread_z": 4.38,
        "atr": 16.08,
    }
    sig2 = strategy.on_bar(
        StrategyContext(
            position=PositionView(size=0),
            market=MarketData(ticker="TMF", last_bar=bar2),
            config=base_ctx.config,
        )
    )
    assert sig2 is not None and sig2.action == "SELL_NEAR_BUY_FAR"


def test_t2_bbo_invalid_and_nonfinite_spread_clear_submitting():
    """T2: ENTRY_BBO_INVALID and COMBO_PRICE_NONFINITE must also clear speculative markers."""
    for label, overrides in (
        ("ENTRY_BBO_INVALID", {"bbo_near_bid": -1.0}),
        ("COMBO_PRICE_NONFINITE", {"spread": float("nan")}),
    ):
        monitor = _make_monitor()
        strat = SimpleNamespace(
            _has_position=False,
            _lifecycle="SUBMITTING",
            _entry_ts=datetime(2026, 11, 24, 10, 58, 44),
            _entry_time_monotonic=123.4,
            _entry_guard_start_ms=1000.0,
            _entry_ts_ms=1000.0,
        )
        _submit_entry(monitor, strat, **overrides)
        assert any(
            ev == "ORDER_REJECTED_LOCAL" and p.get("reason") == label
            for ev, p in monitor.events
        )
        assert strat._lifecycle == "FLAT", f"{label} did not reset _lifecycle"
        assert strat._entry_ts is None
        assert strat._entry_time_monotonic == 0.0
        assert strat._entry_guard_start_ms == 0.0
        assert strat._entry_ts_ms == 0.0


def test_t3_combo_lifecycle_create_failed_behavior_unchanged():
    """T3: Existing COMBO_LIFECYCLE_CREATE_FAILED path still clears speculative markers."""
    monitor = _make_monitor(fail_create=True)
    strat = SimpleNamespace(
        _has_position=False,
        _lifecycle="SUBMITTING",
        _entry_ts=datetime(2026, 11, 24, 10, 58, 44),
        _entry_time_monotonic=123.4,
        _entry_guard_start_ms=1000.0,
        _entry_ts_ms=1000.0,
    )
    _submit_entry(monitor, strat)
    assert any(
        ev == "ORDER_REJECTED_LOCAL" and p.get("reason") == "COMBO_LIFECYCLE_CREATE_FAILED"
        for ev, p in monitor.events
    )
    assert strat._lifecycle == "FLAT"
    assert strat._has_position is False
    assert strat._entry_ts is None
    assert strat._entry_time_monotonic == 0.0
    assert strat._entry_guard_start_ms == 0.0
    assert strat._entry_ts_ms == 0.0


def test_t4_real_open_position_never_cleared_on_entry_reject():
    """T4: When strategy._has_position is True, local entry rejection must NEVER
    clear _has_position or _lifecycle."""
    entry_ts = datetime(2026, 11, 24, 10, 0, 0)
    for overrides in (
        {"bbo_near_bid": None},
        {"bbo_near_bid": -5.0},
        {"spread": float("nan")},
    ):
        monitor = _make_monitor()
        strat = SimpleNamespace(
            _has_position=True,
            _lifecycle="OPEN",
            _entry_ts=entry_ts,
            _entry_time_monotonic=55.5,
            _entry_guard_start_ms=9999.0,
            _entry_ts_ms=9999.0,
        )
        _submit_entry(monitor, strat, **overrides)
        assert strat._has_position is True
        assert strat._lifecycle == "OPEN"
        assert strat._entry_ts == entry_ts
        assert strat._entry_time_monotonic == 55.5
        assert strat._entry_guard_start_ms == 9999.0
        assert strat._entry_ts_ms == 9999.0
