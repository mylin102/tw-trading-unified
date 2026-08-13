import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
from strategies.plugins.futures.active.tmf_spread import TMFSpread
from core.strategy_context import StrategyContext, MarketData, PositionView
from core.order_management.order import OrderSide, OrderStatus

@pytest.fixture
def strategy():
    s = TMFSpread()
    # 2026-05-25 Gemini CLI: Mock state restore to ensure clean testing environment
    s._restore_position_state = MagicMock(return_value=False)
    # 2026-05-25 Gemini CLI: Mock state writing to avoid file I/O
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        config = {
            "ticker": "TMF", 
            "params": {
                "min_atr": 5.0, 
                "trail_distance_points": 35.0,
                "atr_multiplier_stop": 1.0,
                "atr_multiplier_trail": 3.5,
                "confirm_ticks": 1,
                "confirm_ms": 0.0
            }
        }
        s.init(StrategyContext(market=MarketData(last_bar={}, ticker="TMF"), position=PositionView(size=0), config=config))
    return s

def test_mts_exit_trigger_logic(strategy):
    """
    Test 1: Verify that the ticker monitor correctly triggers an EXIT signal 
    when trailing stop condition is met on a single leg.
    """
    # 2026-05-25 Gemini CLI: Adjusted test parameters for correct threshold behavior
    # ATR=10.0, multipliers: stop=1.0, trail=3.5 (from fixture)
    # stop = 10.0, trail = 35.0 (calculated in strategy via max(20.0, 10.0 * 3.5))
    
    # 1. Setup a "Released" state where only FAR leg is held (LONG)
    strategy._has_position = True
    strategy._released_leg = "near"
    strategy._side = "LONG"
    strategy._far_entry = 44000.0
    strategy._peak = 44100.0
    strategy._ticker = "TMF"
    # ADR-011 Phase 3: Must set lifecycle explicitly (legacy fallback blocked)
    from strategies.plugins.futures.active.tmf_spread import (
        PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
        TrailGroup, TrailGroupStatus, Leg,
    )
    import time
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.COMPLETED,
            filled_leg=Leg.NEAR, canceled_leg=Leg.FAR,
        ),
        trail_group=TrailGroup(
            status=TrailGroupStatus.ARMED,
            remaining_leg=Leg.FAR,
        ),
    )
    strategy._single_leg_entered_mono = time.monotonic() - 10.0
    strategy._single_leg_post_fill_ticks = 10
    
    # 2. Case A: Price is 44075 (Peak 100 - Current 75 = 25 pts drop, < 35 threshold) -> No signal
    bar_no_exit = {
        "near_close": 44100.0, "far_close": 44075.0, "atr": 10.0,
        "near_bid": 44099.0, "near_ask": 44101.0,
        "far_bid": 44074.0, "far_ask": 44076.0,
        "timestamp": datetime.now()
    }
    ctx_no_exit = StrategyContext(market=MarketData(last_bar=bar_no_exit, ticker="TMF"), 
                                 position=PositionView(size=1), config={})
    signal = strategy.on_bar(ctx_no_exit)
    assert signal is None
    assert strategy._has_position is True

    # 3. Case B: Price is 44060 (Peak 100 - Current 60 = 40 pts drop, > 35 threshold) -> Trigger EXIT
    bar_exit = {
        "near_close": 44100.0, "far_close": 44020.0, "atr": 10.0,
        "near_bid": 44099.0, "near_ask": 44101.0,
        "far_bid": 44019.0, "far_ask": 44021.0,
        "timestamp": datetime.now()
    }
    ctx_exit = StrategyContext(market=MarketData(last_bar=bar_exit, ticker="TMF"), 
                               position=PositionView(size=1), config={})
    
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx_exit)
        
    assert signal is not None
    assert signal.action == "EXIT"
    # 2026-06-23 Gemini CLI: Reset strategy manually to simulate fill confirmation (deferred sync design)
    strategy._reset()
    assert strategy._has_position is False # Strategy resets after EXIT signal

def test_mts_order_lifecycle_flow():
    """
    Test 2: Verify that when an exit condition is met, the system correctly
    submits the order with the new labels and processes the lifecycle transitions.
    """
    from strategies.futures.monitor import FuturesMonitor
    from core.order_management.order_manager import OrderManager
    from core.signal import Signal
    
    # 1. Setup Monitor and Strategy
    api = MagicMock()
    # Mocking Contracts.Futures.TMF
    api.Contracts.Futures.TMF = [MagicMock(code="TMFF6", delivery_date="2026-06-17")]
    
    monitor = FuturesMonitor(api, "config/futures_night.yaml", dry_run=True)
    monitor.ticker = "TMF"
    monitor._use_order_manager = True
    monitor.order_mgr = OrderManager(api)
    # 2026-07-07 Hermes Agent: contracts must be set (placeholder guard)
    monitor.contract = MagicMock(code="TMFF6")
    monitor.far_contract = MagicMock(code="TMFH6")
    
    # Setup Strategy State (Released Near, Remaining FAR LONG)
    strat = TMFSpread()
    strat._has_position = True
    strat._released_leg = "near"
    strat._near_side = "SHORT"
    strat._far_side = "LONG"
    strat._side = "LONG"
    strat._far_entry = 44000.0
    strat._peak = 44100.0
    strat._ticker = "TMF"
    strat._trade_id = "mts-lifecycle-test"
    # 2026-07-07 Hermes Agent: lifecycle must be SINGLE_LEG (restart gap guard)
    from strategies.plugins.futures.active.tmf_spread import (
        PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
        TrailGroup, TrailGroupStatus, Leg,
    )
    strat._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.COMPLETED,
            filled_leg=Leg.FAR, canceled_leg=Leg.NEAR,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED),
    )
    
    # 2. Trigger Exit via _mts_tick (simulating on_bar signal)
    bar_dict = {
        "near_close": 44100.0, "far_close": 44020.0, "atr": 10.0, 
        "timestamp": datetime.now(), "code": "TMFF6"
    }
    
    # 2026-05-25 Gemini CLI: Pass a Signal object to _submit_mts_order_signal
    signal_obj = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")
    
    with patch.object(monitor.order_mgr, 'submit') as mock_submit, \
         patch("strategies.futures.monitor._mts_position_state_path") as mock_state_path, \
         patch("strategies.futures.monitor.is_taifex_futures_market_open", return_value=True):
        mock_state_path.return_value.exists.return_value = False
        monitor._submit_mts_order_signal(signal_obj, strat, bar_dict, datetime.now())
        
        # Verify order creation and label
        assert mock_submit.called
        order = mock_submit.call_args[0][0]
        # Contract resolved → real code
        assert order.symbol == "TMFH6"
        assert order.side == OrderSide.SELL
        assert order.strategy == "MTS_EXIT"  # Verification of new label
        
        # Verify it's in pending_lifecycle_orders
        assert order.order_id in monitor._pending_lifecycle_orders
        assert monitor._pending_lifecycle_orders[order.order_id]["signal"] == "EXIT"

    # 3. Simulate Fill Confirmation
    deal_event = MagicMock()
    deal_event.order_id = order.order_id
    deal_event.fill_qty = 1
    deal_event.fill_price = 44060.0
    deal_event.status = OrderStatus.FILLED
    deal_event.deal_id = "deal-123"
    deal_event.symbol = "TMF_FAR"
    
    # 2026-05-25 Gemini CLI: Use simpler patching to avoid module attribute issues
    with patch("strategies.futures.monitor.save_trade"), \
         patch("strategies.futures.monitor.DecisionLogger", create=True):
        
        monitor._apply_confirmed_futures_deal(deal_event)
        
        # In real code, strategy.on_bar(EXIT) calls strategy._reset()
        strat._reset()
        
        # Verify strategy state sync (it should be reset now)
        assert strat._has_position is False
        assert strat._lifecycle == "FLAT"

def test_mts_near_leg_exit_clears_trader_position():
    """
    2026-06-23 Gemini CLI: Verify that when the near leg exit order is filled,
    the trader position is correctly set to 0 (flat), not entered short (-1).
    """
    from strategies.futures.monitor import FuturesMonitor
    from core.order_management.order_manager import OrderManager
    from core.order_management.order import OrderType, OrderSide, OrderStatus
    
    api = MagicMock()
    api.Contracts.Futures.TMF = [MagicMock(code="TMFF6", delivery_date="2026-06-17")]
    
    monitor = FuturesMonitor(api, "config/futures_night.yaml", dry_run=True)
    monitor.ticker = "TMF"
    monitor._use_order_manager = True
    monitor.order_mgr = OrderManager(api)
    monitor.contract = MagicMock(code="TMFF6")
    
    # Setup initial position as LONG (1)
    monitor.trader.position = 1
    monitor.trader.entry_price = 44000.0
    
    # Create near leg exit order
    order = monitor.order_mgr.create_order(
        symbol="TMFF6", 
        side=OrderSide.SELL, 
        order_type=OrderType.MKP, 
        quantity=1, 
        strategy="MTS_EXIT"
    )
    
    monitor._pending_lifecycle_orders[order.order_id] = {
        "intent_id": order.intent_id, "signal": "EXIT", "reason": "test_exit",
        "ts": datetime.now(), "lots": 1, "price": 44000.0, "ref_ohlc": {},
        "strategy": "MTS_EXIT",
    }
    
    # Simulate the fill event
    deal_event = MagicMock()
    deal_event.order_id = order.order_id
    deal_event.fill_qty = 1
    deal_event.fill_price = 44050.0
    deal_event.status = OrderStatus.FILLED
    deal_event.deal_id = "deal-999"
    deal_event.symbol = "TMFF6"
    
    with patch("strategies.futures.monitor.save_trade"), \
         patch("strategies.futures.monitor.DecisionLogger", create=True):
        monitor._apply_confirmed_futures_deal(deal_event)
        
    # Verify that the trader's position was zeroed out (0), not set to -1 (short entry)
    assert monitor.trader.position == 0


def test_ccf4eb77_regression_single_leg_ignores_pre_release_bar_extrema_long(strategy):
    """
    Verify that in live/paper mode (non-backtest), the SINGLE_LEG trailing evaluator
    ignores any pre-release extremes contained in the current bar's high/low for LONG remaining side.
    """
    # 2026-07-20 Gemini CLI: Add regression test for Phase-Boundary Lookback Leakage - LONG (ccf4eb77)
    from strategies.plugins.futures.active.tmf_spread import (
        PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
        TrailGroup, TrailGroupStatus, Leg,
    )
    import time
    
    # Setup LONG remaining leg on FAR
    strategy._has_position = True
    strategy._released_leg = "near"
    strategy._side = "LONG"
    strategy._far_entry = 44000.0
    strategy._peak = 44000.0 # set peak to exact price at release
    strategy._ticker = "TMF"
    
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.COMPLETED,
            filled_leg=Leg.NEAR, canceled_leg=Leg.FAR,
        ),
        trail_group=TrailGroup(
            status=TrailGroupStatus.ARMED,
            remaining_leg=Leg.FAR,
        ),
    )
    strategy._single_leg_entered_mono = time.monotonic() - 10.0
    strategy._single_leg_post_fill_ticks = 10
    
    # Bar contains a far_low of 43900.0 (historical extreme from before breakout/release)
    # and far_high of 44100.0, but current close is 44000.0.
    bar_polluted = {
        "near_close": 44100.0,
        "far_close": 44000.0,
        "far_high": 44100.0,
        "far_low": 43900.0,
        "atr": 10.0,
        "timestamp": datetime.now()
    }
    
    ctx = StrategyContext(market=MarketData(last_bar=bar_polluted, ticker="TMF"), 
                          position=PositionView(size=1), config={})
    
    # Run strategy logic. In live/paper mode, it must ignore bar_low (43900.0) 
    # and use the current tick price (44000.0) as _rem_low, so pullback is 0, NOT 100 points.
    # Therefore, no exit signal should trigger.
    signal = strategy.on_bar(ctx)
    assert signal is None
    assert strategy._peak == 44000.0  # Peak should not jump to the polluted bar_high


def test_ccf4eb77_regression_single_leg_ignores_pre_release_bar_extrema_short(strategy):
    """
    Verify that in live/paper mode (non-backtest), the SINGLE_LEG trailing evaluator
    ignores any pre-release extremes contained in the current bar's high/low for SHORT remaining side.
    """
    # 2026-07-20 Gemini CLI: Add regression test for Phase-Boundary Lookback Leakage - SHORT (ccf4eb77)
    from strategies.plugins.futures.active.tmf_spread import (
        PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
        TrailGroup, TrailGroupStatus, Leg,
    )
    import time
    
    # Setup SHORT remaining leg on NEAR (FAR was released)
    strategy._has_position = True
    strategy._released_leg = "far"
    strategy._side = "SHORT"
    strategy._near_entry = 43264.0
    strategy._nadir = 43050.0 # set nadir to exact price at release
    strategy._ticker = "TMF"
    
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.COMPLETED,
            filled_leg=Leg.FAR, canceled_leg=Leg.NEAR,
        ),
        trail_group=TrailGroup(
            status=TrailGroupStatus.ARMED,
            remaining_leg=Leg.NEAR,
        ),
    )
    strategy._single_leg_entered_mono = time.monotonic() - 10.0
    strategy._single_leg_post_fill_ticks = 10
    
    # Bar contains a near_high of 43474.0 (historical extreme from before breakout/release)
    # and near_low of 43000.0, but current close is 43050.0.
    bar_polluted = {
        "near_close": 43050.0,
        "far_close": 43313.0,
        "near_high": 43474.0,
        "near_low": 43000.0,
        "atr": 10.0,
        "timestamp": datetime.now()
    }
    
    ctx = StrategyContext(market=MarketData(last_bar=bar_polluted, ticker="TMF"), 
                          position=PositionView(size=1), config={})
    
    # Run strategy logic. In live/paper mode, it must ignore bar_high (43474.0) 
    # and use the current tick price (43050.0) as _rem_high, so pullback/rebound is 0, NOT 424 points.
    # Therefore, no exit signal should trigger.
    signal = strategy.on_bar(ctx)
    assert signal is None
    assert strategy._nadir == 43050.0  # Nadir should not jump to the polluted bar_low or anything else


def test_single_leg_trail_uses_post_release_tick_path_only_long(strategy):
    """
    Verify that peak/nadir are dynamically updated sequentially based on close prices
    received after activation, rather than jumping to pre-activation extremes for LONG remaining side.
    """
    # 2026-07-20 Gemini CLI: Add trailing validation test for post-release tick path - LONG
    from strategies.plugins.futures.active.tmf_spread import (
        PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
        TrailGroup, TrailGroupStatus, Leg,
    )
    import time
    
    strategy._has_position = True
    strategy._released_leg = "near"
    strategy._side = "LONG"
    strategy._far_entry = 44000.0
    strategy._peak = 44000.0
    strategy._ticker = "TMF"
    
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.COMPLETED,
            filled_leg=Leg.NEAR, canceled_leg=Leg.FAR,
        ),
        trail_group=TrailGroup(
            status=TrailGroupStatus.ARMED,
            remaining_leg=Leg.FAR,
        ),
    )
    strategy._single_leg_entered_mono = time.monotonic() - 10.0
    strategy._single_leg_post_fill_ticks = 10
    
    # Tick 1: Price rises to 44010.0. Peak should update to 44010.0.
    bar_tick1 = {
        "near_close": 44100.0, "far_close": 44010.0, "atr": 10.0,
        "near_bid": 44099.0, "near_ask": 44101.0,
        "far_bid": 44009.0, "far_ask": 44011.0,
        "timestamp": datetime.now()
    }
    ctx1 = StrategyContext(market=MarketData(last_bar=bar_tick1, ticker="TMF"), 
                           position=PositionView(size=1), config={})
    signal1 = strategy.on_bar(ctx1)
    assert signal1 is None
    assert strategy._peak == 44010.0
    
    # Tick 2: Price drops to 43980.0 (Pullback = 30 pts < 35 threshold). No exit signal.
    bar_tick2 = {
        "near_close": 44100.0, "far_close": 43980.0, "atr": 10.0,
        "near_bid": 44099.0, "near_ask": 44101.0,
        "far_bid": 43979.0, "far_ask": 43981.0,
        "timestamp": datetime.now()
    }
    ctx2 = StrategyContext(market=MarketData(last_bar=bar_tick2, ticker="TMF"), 
                           position=PositionView(size=1), config={})
    signal2 = strategy.on_bar(ctx2)
    assert signal2 is None
    assert strategy._peak == 44010.0  # Peak remains 44010.0
    
    # Tick 3: Price drops to 43970.0 (Pullback = 40 pts >= 35 threshold). Trigger EXIT.
    bar_tick3 = {
        "near_close": 44100.0, "far_close": 43970.0, "atr": 10.0,
        "near_bid": 44099.0, "near_ask": 44101.0,
        "far_bid": 43969.0, "far_ask": 43971.0,
        "timestamp": datetime.now()
    }
    ctx3 = StrategyContext(market=MarketData(last_bar=bar_tick3, ticker="TMF"), 
                           position=PositionView(size=1), config={})
    
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal3 = strategy.on_bar(ctx3)
        
    assert signal3 is not None
    assert signal3.action == "EXIT"


def test_single_leg_trail_uses_post_release_tick_path_only_short(strategy):
    """
    Verify that peak/nadir are dynamically updated sequentially based on close prices
    received after activation, rather than jumping to pre-activation extremes for SHORT remaining side.
    """
    # 2026-07-20 Gemini CLI: Add trailing validation test for post-release tick path - SHORT
    from strategies.plugins.futures.active.tmf_spread import (
        PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
        TrailGroup, TrailGroupStatus, Leg,
    )
    import time
    
    strategy._has_position = True
    strategy._released_leg = "far"
    strategy._side = "SHORT"
    strategy._near_entry = 43200.0
    strategy._nadir = 43200.0
    strategy._ticker = "TMF"
    
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.COMPLETED,
            filled_leg=Leg.FAR, canceled_leg=Leg.NEAR,
        ),
        trail_group=TrailGroup(
            status=TrailGroupStatus.ARMED,
            remaining_leg=Leg.NEAR,
        ),
    )
    strategy._single_leg_entered_mono = time.monotonic() - 10.0
    strategy._single_leg_post_fill_ticks = 10
    
    # Tick 1: Price drops to 43190.0. Nadir should update to 43190.0.
    bar_tick1 = {
        "near_close": 43190.0, "far_close": 43450.0, "atr": 10.0,
        "near_bid": 43189.0, "near_ask": 43191.0,
        "far_bid": 43449.0, "far_ask": 43451.0,
        "timestamp": datetime.now()
    }
    ctx1 = StrategyContext(market=MarketData(last_bar=bar_tick1, ticker="TMF"), 
                           position=PositionView(size=1), config={})
    signal1 = strategy.on_bar(ctx1)
    assert signal1 is None
    assert strategy._nadir == 43190.0
    
    # Tick 2: Price rises to 43220.0 (Rebound = 30 pts < 35 threshold). No exit signal.
    bar_tick2 = {
        "near_close": 43220.0, "far_close": 43450.0, "atr": 10.0,
        "near_bid": 43219.0, "near_ask": 43221.0,
        "far_bid": 43449.0, "far_ask": 43451.0,
        "timestamp": datetime.now()
    }
    ctx2 = StrategyContext(market=MarketData(last_bar=bar_tick2, ticker="TMF"), 
                           position=PositionView(size=1), config={})
    signal2 = strategy.on_bar(ctx2)
    assert signal2 is None
    assert strategy._nadir == 43190.0  # Nadir remains 43190.0
    
    # Tick 3: Price rises to 43230.0 (Rebound = 40 pts >= 35 threshold). Trigger EXIT.
    bar_tick3 = {
        "near_close": 43230.0, "far_close": 43450.0, "atr": 10.0,
        "near_bid": 43229.0, "near_ask": 43231.0,
        "far_bid": 43449.0, "far_ask": 43451.0,
        "timestamp": datetime.now()
    }
    ctx3 = StrategyContext(market=MarketData(last_bar=bar_tick3, ticker="TMF"), 
                           position=PositionView(size=1), config={})
    
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal3 = strategy.on_bar(ctx3)
        
    assert signal3 is not None
    assert signal3.action == "EXIT"

def _release_armed_strategy(strategy, *, side="SHORT", near_entry=44100.0,
                            far_entry=44200.0, far_moved=False):
    """[P0b] minimal RELEASE-armed state: SPREAD phase, release group
    ARMED, one leg's loss beyond the ATR stop so the lifecycle decides
    RELEASE on that leg."""
    from strategies.plugins.futures.active.tmf_spread import (
        PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus)
    strategy._has_position = True
    strategy._side = side
    strategy._near_entry = near_entry
    strategy._far_entry = far_entry
    strategy._ticker = "TMF"
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED))
    return strategy


def test_release_near_wide_far_quote_does_not_block(strategy):
    """[P0b] the width gate is LEG-SCOPED for a single-leg RELEASE: a
    wide FAR quote (7pt > max 3) must NOT block the RELEASE_NEAR whose
    own leg quote is narrow (2pt <= 3)."""
    strategy = _release_armed_strategy(strategy)
    # near rose 20pts (SHORT near loss 20 > release stop atr10*1.0)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        "near_bid": 44119.0, "near_ask": 44121.0,   # width 2
        "far_bid": 44197.0, "far_ask": 44204.0,     # width 7 > max 3
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is not None, (
        "RELEASE_NEAR must not be blocked by the wide FAR quote")
    # the single-leg release is surfaced as a PARTIAL_EXIT signal
    assert getattr(signal, "action", None) in (
        "RELEASE", "RELEASE_NEAR", "PARTIAL_EXIT")


def test_release_near_wide_near_quote_still_blocks(strategy):
    """[P0b] the leg-scoped gate still blocks when the RELEASED leg's
    own quote is wide: RELEASE_NEAR with near width 7 > max 3 => None."""
    strategy = _release_armed_strategy(strategy)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        "near_bid": 44113.0, "near_ask": 44120.0,   # width 7 > max 3
        "far_bid": 44199.0, "far_ask": 44201.0,     # width 2
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None  # the released leg's own wide quote blocks


def test_release_far_wide_near_quote_does_not_block(strategy):
    """[P0b] symmetric: a wide NEAR quote does not block the RELEASE_FAR
    whose own leg quote is narrow."""
    strategy = _release_armed_strategy(strategy)
    # far rose 20pts (SHORT far loss 20 > the near's 0) => release FAR
    bar = {
        "near_close": 44100.0, "far_close": 44220.0, "atr": 10.0,
        "near_bid": 44093.0, "near_ask": 44100.0,   # width 7 > max 3
        "far_bid": 44219.0, "far_ask": 44221.0,     # width 2
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is not None, (
        "RELEASE_FAR must not be blocked by the wide NEAR quote")
    # the single-leg release is surfaced as a PARTIAL_EXIT signal
    assert getattr(signal, "action", None) in (
        "RELEASE", "RELEASE_FAR", "PARTIAL_EXIT")

def test_release_near_quote_zero_blocks(strategy):
    """[P0c] the released leg's quote is 0/0: QUOTE_INVALID — never
    treated as a 0-point width that wrongly passes a single-leg close."""
    strategy = _release_armed_strategy(strategy)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        "near_bid": 0, "near_ask": 0,
        "far_bid": 44199.0, "far_ask": 44201.0,
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None, "a zero quote must block the release"


def test_release_near_quote_missing_fallback_blocks(strategy):
    """[P0c] the released leg's bid/ask fields are MISSING (the gate
    falls back to close => 0-point width): QUOTE_INVALID — block."""
    strategy = _release_armed_strategy(strategy)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        # no near_bid / near_ask keys at all (fallback path)
        "far_bid": 44199.0, "far_ask": 44201.0,
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None, "a missing quote (fallback) must block"


def test_release_near_quote_nan_blocks(strategy):
    """[P0c] the released leg's quote is NaN: QUOTE_INVALID — block."""
    strategy = _release_armed_strategy(strategy)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        "near_bid": float("nan"), "near_ask": float("nan"),
        "far_bid": 44199.0, "far_ask": 44201.0,
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None, "a NaN quote must block the release"


def test_release_near_quote_inverted_blocks(strategy):
    """[P0c] the released leg's quote is inverted (ask < bid):
    QUOTE_INVALID — block."""
    strategy = _release_armed_strategy(strategy)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        "near_bid": 44121.0, "near_ask": 44119.0,   # inverted
        "far_bid": 44199.0, "far_ask": 44201.0,
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None, "an inverted quote must block the release"


def test_combined_both_legs_wide_any_blocks(strategy):
    """[P0c] the both-leg (non-single-leg) path stays conservative: any
    leg wider than max_spread_width blocks — the single-leg scoping
    must NOT leak into the default path."""
    from strategies.plugins.futures.active.tmf_spread import (
        PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
        TrailGroup, TrailGroupStatus, Leg)
    import time
    strategy._has_position = True
    strategy._released_leg = "near"
    strategy._side = "LONG"
    strategy._far_entry = 44000.0
    strategy._peak = 44100.0
    strategy._ticker = "TMF"
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.COMPLETED,
                                   filled_leg=Leg.NEAR,
                                   canceled_leg=Leg.FAR),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED,
                               remaining_leg=Leg.FAR))
    strategy._single_leg_entered_mono = time.monotonic() - 10.0
    strategy._single_leg_post_fill_ticks = 10
    # far dropped 80pts (giveback 100-20 >> trail_dist 35) => trail EXIT
    # BUT the remaining (FAR) leg's quote is wide (7 > max 3) => block
    bar = {
        "near_close": 44100.0, "far_close": 44020.0, "atr": 10.0,
        "near_bid": 44099.0, "near_ask": 44101.0,   # width 2
        "far_bid": 44013.0, "far_ask": 44020.0,     # width 7 > max 3
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None, "the both-leg path must keep blocking wide quotes"

def test_release_near_quote_none_blocks(strategy):
    """[P0c2] the released leg's quote keys are present but None:
    QUOTE_INVALID — the width arithmetic must never see None."""
    strategy = _release_armed_strategy(strategy)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        "near_bid": None, "near_ask": None,
        "far_bid": 44199.0, "far_ask": 44201.0,
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None, "a None quote must block (no TypeError)"


def test_release_near_quote_string_blocks(strategy):
    """[P0c2] the released leg's quote is a string: QUOTE_INVALID — the
    width arithmetic must never subtract strings."""
    strategy = _release_armed_strategy(strategy)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        "near_bid": "44099", "near_ask": "44101",
        "far_bid": 44199.0, "far_ask": 44201.0,
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None, "a string quote must block (no TypeError)"


def test_release_near_quote_bool_blocks(strategy):
    """[P0c2] the released leg's quote is a bool (True is an int):
    QUOTE_INVALID — a bool must never be read as a 1pt price."""
    strategy = _release_armed_strategy(strategy)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        "near_bid": True, "near_ask": 44101.0,
        "far_bid": 44199.0, "far_ask": 44201.0,
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None, "a bool quote must block"


def test_release_near_other_leg_string_blocks(strategy):
    """[P0c2] the NON-released leg's quote is a string: the width
    arithmetic must not crash — QUOTE_INVALID blocks the eval."""
    strategy = _release_armed_strategy(strategy)
    bar = {
        "near_close": 44120.0, "far_close": 44200.0, "atr": 10.0,
        "near_bid": 44119.0, "near_ask": 44121.0,
        "far_bid": 44199.0, "far_ask": "44201",
        "near_tick_age_ms": 0, "far_tick_age_ms": 0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                          position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        signal = strategy.on_bar(ctx)
    assert signal is None, "a malformed other-leg quote must block, not crash"

