"""2026-11-26 Gemini CLI: Contract & regression tests for near_trailing_tp (T1-T7).

Drives the real TMFSpread.on_bar() path so exit-side BBO wiring, EXIT_LOG telemetry,
per-trade peak isolation, and fail-closed config fallback are all verified end-to-end.
"""
from datetime import datetime
from unittest.mock import patch

from core.strategy_context import MarketData, PositionView, StrategyContext
from strategies.plugins.futures.active.tmf_spread import (
    Leg,
    PositionLifecycle,
    PositionPhase,
    ReleaseGroup,
    ReleaseGroupStatus,
    TMFSpread,
    TrailGroup,
    TrailGroupStatus,
)


def _make_bar(near_close, far_close, **kw):
    bar = {
        "near_close": near_close, "far_close": far_close, "atr": 180.0,
        "near_bid": near_close, "near_ask": near_close + 2.0,
        "far_bid": far_close, "far_ask": far_close + 10.0,
        "bbo_near_bid": near_close, "bbo_near_ask": near_close + 2.0,
        "bbo_far_bid": far_close, "bbo_far_ask": far_close + 10.0,
        "bbo_near_age_ms": 10, "bbo_far_age_ms": 20,
        "near_high": near_close + 20, "near_low": near_close - 20,
        "far_high": far_close + 20, "far_low": far_close - 20,
        "near_tick_age_ms": 0.0, "far_tick_age_ms": 0.0,
        "spread_z": 0.0, "timestamp": datetime.now(),
        "near_vwap": near_close, "far_vwap": far_close,
        "sqz_on": False,
    }
    bar.update(kw)
    for leg in ("near", "far"):
        for side in ("bid", "ask"):
            bk = f"bbo_{leg}_{side}"
            if bk in kw and f"{leg}_{side}" not in kw:
                bar[f"{leg}_{side}"] = kw[bk]
    return bar


def _make_strategy(tmp_path, *, near_trailing_cfg=None, near_side="LONG", far_side="SHORT",
                   near_entry=39000.0, far_entry=39100.0, trade_id="mts-auto-20261126-170000-001",
                   phase=PositionPhase.SPREAD, released_leg=None):
    s = TMFSpread()
    ilb_cfg = {"enabled": True, "take_profit_pts": 100.0, "stop_loss_pts": 30.0}
    if near_trailing_cfg is not None:
        ilb_cfg["near_trailing_tp"] = near_trailing_cfg
    config = {"ticker": "TMF", "params": {
        "atr_multiplier_stop": 2.5, "atr_multiplier_trail": 1.0,
        "release_stop_points": 88, "trail_distance_points": 66,
        "confirm_ticks": 1, "confirm_ms": 0, "max_quote_age_ms": 999999,
        "max_spread_width": 999999, "mfe_tighten": {"enabled": False},
        "post_release": {"breakeven_after_atr": 999, "force_lock_after_atr": 999},
        "independent_leg_bracket": ilb_cfg,
    }}
    init_bar = _make_bar(near_entry, far_entry)
    s.init(StrategyContext(market=MarketData(last_bar=init_bar, ticker="TMF"),
                           position=PositionView(size=0), config=config))
    s._state_file = str(tmp_path / "state.json")
    s._has_position = True
    s._near_entry = near_entry
    s._far_entry = far_entry
    s._near_side = near_side
    s._far_side = far_side
    s._side = near_side
    s._trade_id = trade_id
    s._entry_ts = datetime.now()
    s._peak, s._nadir = near_entry, far_entry
    s._release_near_ticks = s._release_far_ticks = 0
    s._release_near_start_time = s._release_far_start_time = 0.0
    s._release_mono = 0.0
    if phase == PositionPhase.SPREAD:
        s._lifecycle = "OPEN"
        s._released_leg = None
        s._lifecycle_oca = PositionLifecycle(
            phase=PositionPhase.SPREAD,
            release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
            trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
        )
    else:
        s._lifecycle = "SINGLE_LEG"
        s._released_leg = released_leg or "far"
        s._lifecycle_oca = PositionLifecycle(
            phase=PositionPhase.SINGLE_LEG,
            release_group=ReleaseGroup(
                status=ReleaseGroupStatus.FILLED,
                filled_leg=Leg.FAR if s._released_leg == "far" else Leg.NEAR,
            ),
            trail_group=TrailGroup(
                status=TrailGroupStatus.ARMED,
                remaining_leg=Leg.NEAR if s._released_leg == "far" else Leg.FAR,
            ),
        )
    return s, config


def _drive(s, config, bar, events, size=2):
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event",
               side_effect=lambda ev, **kw: events.append((ev, kw))):
        return s.on_bar(StrategyContext(
            market=MarketData(last_bar=bar, ticker="TMF"),
            position=PositionView(size=size),
            config=config,
        ))


def test_t1_near_reaches_100_then_gives_back_20_exits_using_bbo_not_close(tmp_path):
    """T1: NEAR LONG reaches +125 then BBO bid drops to +105 (giveback=20) while close=+115 -> exits on BBO."""
    s, cfg = _make_strategy(
        tmp_path,
        near_trailing_cfg={"enabled": True, "activate_pts": 100.0, "giveback_pts": 20.0},
        near_side="LONG", far_side="SHORT", near_entry=39000.0, far_entry=39100.0,
    )
    events = []
    # Tick 1: NEAR BBO bid = 39125 (+125) -> arms trailing TP, peak=125, must NOT exit yet
    sig1 = _drive(s, cfg, _make_bar(39125.0, 39100.0, bbo_near_bid=39125.0, bbo_near_ask=39127.0), events)
    assert sig1 is None
    assert not any(ev == "EXIT_LOG" for ev, _ in events)

    # Tick 2: NEAR close = 39115 (+115, giveback only 10 on close), BUT BBO bid = 39105 (+105, giveback=20 on BBO!)
    sig2 = _drive(s, cfg, _make_bar(39115.0, 39100.0, bbo_near_bid=39105.0, bbo_near_ask=39117.0), events)
    assert sig2 is not None
    exit_logs = [kw for ev, kw in events if ev == "EXIT_LOG"]
    assert len(exit_logs) == 1
    assert exit_logs[0]["exit_reason"] == "INDEPENDENT_LEG_BRACKET_TRAILING_TP"
    assert exit_logs[0]["trigger_price"] == 39105.0
    assert exit_logs[0]["trigger_pnl"] == 105.0
    assert exit_logs[0]["trigger_basis"] == "BBO_BID"


def test_t2_near_reaches_100_gives_back_15_does_not_exit(tmp_path):
    """T2: NEAR reaches +120 then gives back only 15 (+105) -> MUST NOT exit."""
    s, cfg = _make_strategy(
        tmp_path,
        near_trailing_cfg={"enabled": True, "activate_pts": 100.0, "giveback_pts": 20.0},
        near_side="LONG", far_side="SHORT", near_entry=39000.0, far_entry=39100.0,
    )
    events = []
    assert _drive(s, cfg, _make_bar(39120.0, 39100.0, bbo_near_bid=39120.0), events) is None
    assert _drive(s, cfg, _make_bar(39105.0, 39100.0, bbo_near_bid=39105.0), events) is None
    assert not any(ev == "EXIT_LOG" for ev, _ in events)


def test_t3_near_never_reaches_100_does_not_trailing_exit(tmp_path):
    """T3: NEAR peaks at +95 then drops to +60 (35 pt drop, never reached 100) -> NO exit."""
    s, cfg = _make_strategy(
        tmp_path,
        near_trailing_cfg={"enabled": True, "activate_pts": 100.0, "giveback_pts": 20.0},
        near_side="LONG", far_side="SHORT", near_entry=39000.0, far_entry=39100.0,
    )
    events = []
    assert _drive(s, cfg, _make_bar(39095.0, 39100.0, bbo_near_bid=39095.0), events) is None
    assert _drive(s, cfg, _make_bar(39060.0, 39100.0, bbo_near_bid=39060.0), events) is None
    assert not any(ev == "EXIT_LOG" for ev, _ in events)


def test_t4_far_leg_remains_fixed_tp100_sl30(tmp_path):
    """T4: FAR leg stays fixed TP=100 / SL=30 even when near_trailing_tp is enabled."""
    s, cfg = _make_strategy(
        tmp_path / "tp",
        near_trailing_cfg={"enabled": True, "activate_pts": 100.0, "giveback_pts": 20.0},
        near_side="SHORT", far_side="LONG", near_entry=39000.0, far_entry=39100.0,
        phase=PositionPhase.SINGLE_LEG, released_leg="near",
    )
    events = []
    sig = _drive(s, cfg, _make_bar(39000.0, 39200.0, bbo_far_bid=39200.0, bbo_far_ask=39210.0), events, size=1)
    assert sig is not None
    exit_logs = [kw for ev, kw in events if ev == "EXIT_LOG"]
    assert len(exit_logs) == 1
    assert exit_logs[0]["exit_reason"] == "INDEPENDENT_LEG_BRACKET_TAKE_PROFIT"

    s2, cfg2 = _make_strategy(
        tmp_path / "sl",
        near_trailing_cfg={"enabled": True, "activate_pts": 100.0, "giveback_pts": 20.0},
        near_side="SHORT", far_side="LONG", near_entry=39000.0, far_entry=39100.0,
        phase=PositionPhase.SINGLE_LEG, released_leg="near",
    )
    events2 = []
    sig2 = _drive(s2, cfg2, _make_bar(39000.0, 39070.0, bbo_far_bid=39070.0, bbo_far_ask=39080.0), events2, size=1)
    assert sig2 is not None
    exit_logs2 = [kw for ev, kw in events2 if ev == "EXIT_LOG"]
    assert len(exit_logs2) == 1
    assert exit_logs2[0]["exit_reason"] == "INDEPENDENT_LEG_BRACKET_STOP_LOSS"


def test_t5_disabled_near_trailing_tp_keeps_fixed_100(tmp_path):
    """T5: enabled=False -> NEAR immediately exits on fixed TP=100."""
    s, cfg = _make_strategy(
        tmp_path,
        near_trailing_cfg={"enabled": False, "activate_pts": 100.0, "giveback_pts": 20.0},
        near_side="LONG", far_side="SHORT", near_entry=39000.0, far_entry=39100.0,
    )
    events = []
    sig = _drive(s, cfg, _make_bar(39100.0, 39100.0, bbo_near_bid=39100.0), events)
    assert sig is not None
    exit_logs = [kw for ev, kw in events if ev == "EXIT_LOG"]
    assert len(exit_logs) == 1
    assert exit_logs[0]["exit_reason"] == "INDEPENDENT_LEG_BRACKET_TAKE_PROFIT"


def test_t6_invalid_params_fail_closed_to_fixed_tp_and_emit_event(tmp_path):
    """T6: giveback<=0 or activate<=0 -> fail-closed fallback to fixed TP=100 + emit warning event."""
    for bad_cfg in (
        {"enabled": True, "activate_pts": 100.0, "giveback_pts": 0.0},
        {"enabled": True, "activate_pts": -10.0, "giveback_pts": 20.0},
    ):
        s, cfg = _make_strategy(
            tmp_path / f"bad_{bad_cfg['activate_pts']}_{bad_cfg['giveback_pts']}",
            near_trailing_cfg=bad_cfg,
            near_side="LONG", far_side="SHORT", near_entry=39000.0, far_entry=39100.0,
        )
        events = []
        sig = _drive(s, cfg, _make_bar(39100.0, 39100.0, bbo_near_bid=39100.0), events)
        assert sig is not None
        assert any(ev == "ILB_NEAR_TRAILING_TP_CONFIG_INVALID" for ev, _ in events)
        exit_logs = [kw for ev, kw in events if ev == "EXIT_LOG"]
        assert len(exit_logs) == 1
        assert exit_logs[0]["exit_reason"] == "INDEPENDENT_LEG_BRACKET_TAKE_PROFIT"


def test_t7_peak_resets_across_consecutive_trades(tmp_path):
    """T7: Trade 1 builds peak=+150; Trade 2 starts at +10 -> MUST NOT inherit Trade 1's peak or exit."""
    s, cfg = _make_strategy(
        tmp_path,
        near_trailing_cfg={"enabled": True, "activate_pts": 100.0, "giveback_pts": 20.0},
        near_side="LONG", far_side="SHORT", near_entry=39000.0, far_entry=39100.0,
        trade_id="mts-auto-20261126-170000-001",
    )
    events1 = []
    assert _drive(s, cfg, _make_bar(39150.0, 39100.0, bbo_near_bid=39150.0), events1) is None

    # Reset and start Trade 2 with a new trade_id
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"):
        s._reset(reason="TEST_RESET")
        s.sync_position(
            side="LONG", near_entry=39200.0, far_entry=39300.0,
            trade_id="mts-auto-20261126-171000-002",
        )

    events2 = []
    # Trade 2 NEAR at +10 (39210): if Trade 1's peak=150 leaked, giveback=140>=20 would wrongly exit!
    sig2 = _drive(s, cfg, _make_bar(39210.0, 39300.0, bbo_near_bid=39210.0, bbo_near_ask=39212.0), events2)
    assert sig2 is None
    assert not any(ev == "EXIT_LOG" for ev, _ in events2)


def test_t7b_trade_id_change_without_reset_clears_peak(tmp_path):
    """T7b: Directly switching _trade_id WITHOUT calling _reset()/sync_position() must still clear _ilb_near_trail_peak."""
    s, cfg = _make_strategy(
        tmp_path,
        near_trailing_cfg={"enabled": True, "activate_pts": 100.0, "giveback_pts": 20.0},
        near_side="LONG", far_side="SHORT", near_entry=39000.0, far_entry=39100.0,
        trade_id="mts-auto-20261126-170000-001",
    )
    events1 = []
    assert _drive(s, cfg, _make_bar(39150.0, 39100.0, bbo_near_bid=39150.0), events1) is None
    assert s._ilb_near_trail_peak == 150.0

    # Bypass _reset() and sync_position() entirely — switch _trade_id & entries directly
    s._trade_id = "mts-auto-20261126-171000-002"
    s._near_entry = 39200.0
    s._far_entry = 39300.0

    events2 = []
    sig2 = _drive(s, cfg, _make_bar(39210.0, 39300.0, bbo_near_bid=39210.0, bbo_near_ask=39212.0), events2)
    assert sig2 is None
    assert s._ilb_near_trail_peak is None
    assert s._ilb_near_trail_trade_id == "mts-auto-20261126-171000-002"
    assert not any(ev == "EXIT_LOG" for ev, _ in events2)
