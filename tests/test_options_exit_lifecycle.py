"""Test options exit lifecycle: manage_open_position triggers + exit_paper_position flow.

2026-05-25 Hermes Agent: initial implementation.

Tests:
1. manage_open_position() returns True when stop_loss threshold is breached
   → exit_paper_position() is called with correct action and price
2. manage_open_position() returns True when trailing stop triggers
3. manage_open_position() returns True when score reversal triggers
4. After exit_paper_position(), position is cleared, ledger is written
5. SessionGuard blocks exit when market is closed
6. QuoteGuard blocks exit when quote is invalid
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# Add options_engine path so imports from strategies/options/ resolve
_options_path = str(Path(__file__).parent.parent / "strategies" / "options")
if _options_path not in sys.path:
    sys.path.insert(0, _options_path)


@pytest.fixture
def mock_monitor():
    """Build a minimal OptionsSqueezeMonitor mock with exit-critical attributes set."""
    monitor = MagicMock()

    # ── Position state (simulate open Call, entry=1470) ──
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.entry_mtx_price = 44088.0
    monitor.has_tp1_hit = False
    monitor.peak_premium = 1470.0  # never went higher
    monitor.stop_loss_price = 0.0
    monitor.trailing_stop_pct = 0.15
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.score_floor = 20.0
    monitor.entry_score = 86.7
    monitor.last_signal = None
    monitor.live_trading = False
    monitor.opening_grace_mins = 5
    monitor.entry_time = None
    monitor.cooldown_until = 0
    monitor.max_holding_days = None
    monitor.min_dte_to_exit = None
    monitor._exit_in_progress = False
    monitor.exit_paper_position = MagicMock()
    monitor.exit_live_position = MagicMock()
    monitor._record_paper_order = MagicMock(return_value={"order_id": "mock-001"})
    monitor.log_trade = MagicMock()

    # ── market_data ──
    monitor.market_data = {
        "MTX": {"close": 44088.0, "bid": 44088.0, "ask": 44090.0},
        "C": {"close": 1470.0, "bid": 1470.0, "ask": 1480.0},
        "P": {"close": 0.0, "bid": 0.0, "ask": 0.0},
    }
    monitor.active_contracts = {}

    def fake_current_option_quote(side):
        q = monitor.market_data.get(side, {})
        bid = float(q.get("bid", 0) or 0)
        ask = float(q.get("ask", 0) or 0)
        close = float(q.get("close", 0) or 0)
        if bid <= 0:
            bid = close
        if ask <= 0:
            ask = close
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else close
        return {"bid": bid, "ask": ask, "mid": mid, "close": close}

    monitor.current_option_quote = fake_current_option_quote

    def fake_validate_quote(side, context="GENERIC"):
        q = fake_current_option_quote(side)
        return {
            "valid": q["bid"] > 0 and q["ask"] > 0 and q["mid"] > 0 and q["ask"] > q["bid"],
            "reason": "OK",
            "bid": q["bid"],
            "ask": q["ask"],
            "mid": q["mid"],
            "spread_ratio": None,
            "max_spread_ratio": 0.3,
        }

    monitor.validate_quote = fake_validate_quote

    def fake_is_market_open(now=None):
        return True, "NIGHT"

    monitor._is_market_open = fake_is_market_open

    def fake_current_strategy_time():
        import datetime
        return datetime.datetime.now()

    monitor._current_strategy_time = fake_current_strategy_time
    monitor._update_theta_release_confirmation = MagicMock(
        return_value={"confirmed": True, "reason": "ok"}
    )

    return monitor


def _signal(score=0.0, timestamp=None):
    """Build a minimal signal dict."""
    import datetime
    return {
        "score": score,
        "timestamp": timestamp or datetime.datetime.now(),
        "side": "C",
        "action": "buy",
    }


# ═══════════════════════════════════════════════════════════════
# Test 1: manage_open_position → stop_loss triggers exit
# ═══════════════════════════════════════════════════════════════

def test_stop_loss_triggers_exit_on_monitor():
    """When exit_price falls below stop_loss threshold (entry * (1 - 0.10)),
    manage_open_position() should return True and call exit_paper_position()."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.entry_mtx_price = 44088.0
    monitor.has_tp1_hit = False
    monitor.peak_premium = 1470.0
    monitor.trailing_stop_pct = 0.15
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.score_floor = 20.0
    monitor.entry_score = 86.7
    monitor.last_signal = None
    monitor.live_trading = False
    monitor.opening_grace_mins = 5
    monitor.entry_time = None
    monitor.cooldown_until = 0
    monitor.max_holding_days = None
    monitor.min_dte_to_exit = None
    monitor._exit_in_progress = False
    monitor.active_contracts = {}
    monitor.market_data = {
        "MTX": {"close": 43000.0, "bid": 43000.0, "ask": 43002.0},
        "C": {"close": 1200.0, "bid": 1200.0, "ask": 1210.0},
    }
    monitor.replay_stats = {"exits": 0}

    monitor.current_option_quote = MagicMock(
        return_value={"bid": 1200.0, "ask": 1210.0, "mid": 1205.0, "close": 1200.0}
    )
    monitor.validate_quote = MagicMock(
        return_value={"valid": True, "reason": "OK", "bid": 1200.0, "ask": 1210.0, "mid": 1205.0}
    )
    monitor._is_market_open = MagicMock(return_value=(True, "NIGHT"))
    monitor._current_strategy_time = MagicMock(return_value=__import__("datetime").datetime.now())
    monitor._update_theta_release_confirmation = MagicMock(
        return_value={"confirmed": True, "reason": "ok"}
    )
    monitor.exit_paper_position = MagicMock()
    monitor.exit_live_position = MagicMock()
    monitor.m_cfg = {"tp1_pct": 0.3}

    # Act — bid=1200 < 1470*0.9=1323 → stop loss should fire
    signal = _signal(score=10.0)
    result = monitor.manage_open_position(signal)

    # Assert
    assert result is True, "manage_open_position should return True when stop loss breached"
    monitor.exit_paper_position.assert_called_once()
    call_args = monitor.exit_paper_position.call_args[0]
    action = call_args[0]
    assert "PAPER_EXIT" in action or "PAPER" in action, f"Expected PAPER exit, got {action}"
    price = call_args[1]
    assert price == 1200.0, f"Expected exit price 1200.0, got {price}"


# ═══════════════════════════════════════════════════════════════
# Test 2: exit_paper_position clears position and writes ledger
# ═══════════════════════════════════════════════════════════════

def test_exit_paper_position_clears_position_state():
    """After exit_paper_position(), position=0, active_side=None, entry_price=0."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    # Set pre-exit state
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.entry_mtx_price = 44088.0
    monitor.entry_time = __import__("datetime").datetime.now()
    monitor.has_tp1_hit = False
    monitor.stop_loss_price = 1323.0
    monitor.peak_premium = 1470.0
    monitor.live_trading = False
    monitor._exit_in_progress = False
    monitor.cooldown_bars = 3
    monitor.cooldown_until = 0
    monitor.replay_stats = {"exits": 0}
    monitor.paper_lots = 2
    monitor.mode = "test"

    # Patch methods exit_paper_position calls
    monitor._record_paper_order = MagicMock(
        return_value={"order_id": "mock-exit-001", "status": "filled"}
    )
    monitor.log_trade = MagicMock()

    # Act
    monitor.exit_paper_position("PAPER_EXIT", 1200.0, "stop_loss score=10.0")

    # Assert state cleared
    assert monitor.position == 0, f"Expected position=0, got {monitor.position}"
    assert monitor.active_side is None, f"Expected active_side=None, got {monitor.active_side}"
    assert monitor.entry_price == 0.0, f"Expected entry_price=0, got {monitor.entry_price}"
    assert monitor.has_tp1_hit is False
    assert monitor.stop_loss_price == 0.0
    assert monitor.peak_premium == 0.0
    assert monitor.entry_time is None

    # Assert side-effects happened
    monitor._record_paper_order.assert_called_once()
    monitor.log_trade.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# Test 3: trailing stop triggers exit
# ═══════════════════════════════════════════════════════════════

def test_trailing_stop_triggers_exit():
    """When premium fell from peak by >trailing_stop_pct, exit should fire."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    # Position: entry 1470, peak 1600 (rose 8.8% → trailing activated)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.has_tp1_hit = False
    monitor.peak_premium = 1600.0  # peak reached
    monitor.trailing_stop_pct = 0.15
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.score_floor = 20.0
    monitor.entry_score = 86.7
    monitor.last_signal = None
    monitor.live_trading = False
    monitor.opening_grace_mins = 5
    monitor.entry_time = None
    monitor.entry_mtx_price = 44088.0
    monitor.cooldown_until = 0
    monitor.max_holding_days = None
    monitor.min_dte_to_exit = None
    monitor._exit_in_progress = False
    monitor.active_contracts = {}
    monitor.market_data = {
        "C": {"close": 1350.0, "bid": 1350.0, "ask": 1360.0},
    }
    monitor.replay_stats = {"exits": 0}

    monitor.current_option_quote = MagicMock(
        return_value={"bid": 1350.0, "ask": 1360.0, "mid": 1355.0, "close": 1350.0}
    )
    monitor.validate_quote = MagicMock(
        return_value={"valid": True, "reason": "OK", "bid": 1350.0, "ask": 1360.0}
    )
    monitor._is_market_open = MagicMock(return_value=(True, "NIGHT"))
    monitor._current_strategy_time = MagicMock(return_value=__import__("datetime").datetime.now())
    monitor._update_theta_release_confirmation = MagicMock(
        return_value={"confirmed": True, "reason": "ok"}
    )
    monitor.exit_paper_position = MagicMock()
    monitor.m_cfg = {"tp1_pct": 0.3}

    # trail_floor = 1600 * (1 - 0.15) = 1360; exit_price = 1350 (bid) < 1360 → trigger
    # Also unrealized_pct = (1600-1470)/1470 = 0.088 > 0.08 → trailing activated
    signal = _signal(score=40.0)
    result = monitor.manage_open_position(signal)

    assert result is True
    monitor.exit_paper_position.assert_called_once()
    call_args = monitor.exit_paper_position.call_args[0]
    assert "TRAIL" in call_args[0].upper(), f"Expected TRAIL exit, got {call_args[0]}"
    assert call_args[1] == 1350.0, f"Expected price 1350, got {call_args[1]}"


# ═══════════════════════════════════════════════════════════════
# Test 4: score reversal triggers exit (Call → bearish)
# ═══════════════════════════════════════════════════════════════

def test_score_reversal_triggers_exit():
    """When signal_score flips negative for a Call position, reversal exit fires."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.entry_score = 86.7
    monitor.has_tp1_hit = False
    monitor.peak_premium = 1470.0
    monitor.trailing_stop_pct = 0.15
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.score_floor = 20.0
    monitor.last_signal = None
    monitor.live_trading = False
    monitor.opening_grace_mins = 5
    # 2026-06-23 Gemini CLI: Set entry_time 10 minutes in the past to avoid opening grace period block
    monitor.entry_time = __import__("datetime").datetime.now() - __import__("datetime").timedelta(minutes=10)
    monitor.entry_mtx_price = 44088.0
    monitor.cooldown_until = 0
    monitor.max_holding_days = None
    monitor.min_dte_to_exit = None
    monitor._exit_in_progress = False
    monitor.active_contracts = {}
    monitor.market_data = {
        "C": {"close": 1460.0, "bid": 1460.0, "ask": 1470.0},
        "MTX": {"close": 43800.0},
    }
    monitor.replay_stats = {"exits": 0}

    monitor.current_option_quote = MagicMock(
        return_value={"bid": 1460.0, "ask": 1470.0, "mid": 1465.0, "close": 1460.0}
    )
    monitor.validate_quote = MagicMock(
        return_value={"valid": True, "reason": "OK", "bid": 1460.0, "ask": 1470.0}
    )
    monitor._is_market_open = MagicMock(return_value=(True, "NIGHT"))
    monitor._current_strategy_time = MagicMock(return_value=__import__("datetime").datetime.now())

    # reversal_threshold = 86.7 * 1.5 = 130.05
    # signal_score = -140 → -140 <= -130.05 → reversal triggered
    monitor._update_theta_release_confirmation = MagicMock(
        return_value={"confirmed": True, "reason": "ok"}
    )
    monitor.exit_paper_position = MagicMock()
    monitor.m_cfg = {"tp1_pct": 0.3}

    # 2026-06-23 Gemini CLI: Pass a fixed 10:00 AM timestamp so that session_mins is >= 5, avoiding opening grace period blocks
    import datetime
    test_time = datetime.datetime(2026, 6, 23, 10, 0)
    signal = _signal(score=-140.0, timestamp=test_time)
    result = monitor.manage_open_position(signal)

    assert result is True
    monitor.exit_paper_position.assert_called_once()
    call_args = monitor.exit_paper_position.call_args[0]
    assert "REVERSAL" in call_args[0].upper(), f"Expected REVERSAL exit, got {call_args[0]}"


# ═══════════════════════════════════════════════════════════════
# Test 5: no exit when market closed (SessionGuard)
# ═══════════════════════════════════════════════════════════════

def test_no_exit_when_market_closed():
    """SessionGuard should block exit when market is closed."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.score_floor = 20.0
    monitor.entry_score = 86.7
    monitor.last_signal = None
    monitor.live_trading = False
    monitor.opening_grace_mins = 5
    monitor.entry_time = None
    monitor.cooldown_until = 0
    monitor._exit_in_progress = False
    monitor.active_contracts = {}
    monitor.replay_stats = {}
    # 2026-06-23 Gemini CLI: Initialize peak_premium to prevent AttributeError in audit print
    monitor.peak_premium = 1470.0
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.trailing_stop_pct = 0.15
    monitor.has_tp1_hit = False

    monitor.current_option_quote = MagicMock(
        return_value={"bid": 1300.0, "ask": 1310.0, "mid": 1305.0, "close": 1300.0}
    )
    monitor.validate_quote = MagicMock(
        return_value={"valid": True, "reason": "OK", "bid": 1300.0, "ask": 1310.0}
    )
    # Market closed
    monitor._is_market_open = MagicMock(return_value=(False, "CLOSED"))
    monitor._current_strategy_time = MagicMock(return_value=__import__("datetime").datetime.now())
    monitor.exit_paper_position = MagicMock()

    signal = _signal(score=10.0)
    result = monitor.manage_open_position(signal)

    assert result is False, "Should NOT exit when market is closed"
    monitor.exit_paper_position.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# Test 6: no exit when quote invalid (QuoteGuard)
# ═══════════════════════════════════════════════════════════════

def test_no_exit_when_quote_invalid():
    """QuoteGuard should block exit when bid/ask is missing or crossed."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.score_floor = 20.0
    monitor.entry_score = 86.7
    monitor.last_signal = None
    monitor.live_trading = False
    monitor.opening_grace_mins = 5
    monitor.entry_time = None
    monitor.cooldown_until = 0
    monitor._exit_in_progress = False
    monitor.active_contracts = {}
    monitor.replay_stats = {}
    # 2026-06-23 Gemini CLI: Initialize peak_premium to prevent AttributeError in audit print
    monitor.peak_premium = 1470.0
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.trailing_stop_pct = 0.15
    monitor.has_tp1_hit = False

    monitor.current_option_quote = MagicMock(
        return_value={"bid": 0.0, "ask": 0.0, "mid": 0.0, "close": 0.0}  # missing quote
    )
    monitor.validate_quote = MagicMock(
        return_value={"valid": False, "reason": "MISSING_QUOTE", "bid": 0.0, "ask": 0.0}
    )
    monitor._is_market_open = MagicMock(return_value=(True, "NIGHT"))
    monitor._current_strategy_time = MagicMock(return_value=__import__("datetime").datetime.now())
    monitor.exit_paper_position = MagicMock()

    signal = _signal(score=10.0)
    result = monitor.manage_open_position(signal)

    assert result is False, "Should NOT exit when quote is invalid"
    monitor.exit_paper_position.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 2026-05-26 Hermes Agent: tick-level exit evaluator tests
# ═══════════════════════════════════════════════════════════════

class FakeTick:
    """Minimal tick stub matching TickFOPv1 interface."""
    def __init__(self, bid_price=0.0, close=0.0, code=""):
        self.bid_price = bid_price
        self.close = close
        self.code = code


def test_option_tick_exit_sets_pending_request_on_stop_loss():
    """Tick premium below stop loss → sets _pending_exit_request, NO direct exit call."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.peak_premium = 1470.0
    monitor.stop_loss_pct = 0.10  # sl = 1323
    monitor.hard_stop_pct = 0.20
    monitor.trailing_stop_pct = 0.15
    monitor.has_tp1_hit = False
    monitor._exit_in_progress = False
    monitor._pending_exit_request = None

    tick = FakeTick(bid_price=1300.0, close=1300.0, code="TXO")
    monitor._option_exit_on_tick(tick)

    assert monitor._pending_exit_request is not None
    assert monitor._pending_exit_request["reason"] == "PAPER_STOP_LOSS"
    assert monitor._pending_exit_request["premium"] == 1300.0
    assert monitor._pending_exit_request["source"] == "OPTION_TICK_EXIT"
    # 2026-06-23 Gemini CLI: _option_exit_on_tick only sets pending request, does not set _exit_in_progress to True
    assert monitor._exit_in_progress is False


def test_option_tick_exit_does_not_set_request_when_exit_in_progress():
    """_exit_in_progress=True → tick exit is skipped."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.peak_premium = 1470.0
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.trailing_stop_pct = 0.15
    monitor.has_tp1_hit = False
    monitor._exit_in_progress = True  # << exit already in progress
    monitor._pending_exit_request = None

    tick = FakeTick(bid_price=1300.0, close=1300.0)
    monitor._option_exit_on_tick(tick)

    assert monitor._pending_exit_request is None, "Should NOT set request when exit in progress"


def test_option_tick_exit_does_not_set_request_when_pending_exists():
    """Existing _pending_exit_request → tick exit is skipped (no overwrite)."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.peak_premium = 1470.0
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.trailing_stop_pct = 0.15
    monitor.has_tp1_hit = False
    monitor._exit_in_progress = False
    monitor._pending_exit_request = {"reason": "PENDING", "premium": 1200.0, "source": "PREVIOUS"}

    tick = FakeTick(bid_price=1000.0, close=1000.0)
    monitor._option_exit_on_tick(tick)

    assert monitor._pending_exit_request["reason"] == "PENDING"
    assert monitor._pending_exit_request["premium"] == 1200.0  # unchanged


def test_option_tick_exit_ignores_when_no_position():
    """_option_exit_on_tick does nothing when position=0."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 0  # << no position
    monitor.active_side = None
    monitor.entry_price = 1470.0
    monitor.peak_premium = 1470.0
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.trailing_stop_pct = 0.15
    monitor.has_tp1_hit = False
    monitor._exit_in_progress = False
    monitor._pending_exit_request = None

    tick = FakeTick(bid_price=1000.0, close=1000.0)
    monitor._option_exit_on_tick(tick)

    assert monitor._pending_exit_request is None


def test_option_peak_premium_updates_on_tick():
    """peak_premium must update on tick even when no exit triggered."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor.peak_premium = 1470.0  # starting peak
    monitor.stop_loss_pct = 0.10
    monitor.hard_stop_pct = 0.20
    monitor.trailing_stop_pct = 0.15
    monitor.has_tp1_hit = False
    monitor._exit_in_progress = False
    monitor._pending_exit_request = None

    # Tick with higher premium → peak_premium should update
    tick = FakeTick(bid_price=1550.0, close=1550.0)
    monitor._option_exit_on_tick(tick)

    assert monitor.peak_premium == 1550.0, f"Expected peak=1550, got {monitor.peak_premium}"
    assert monitor._pending_exit_request is None  # no exit triggered

    # Tick with even higher premium → peak updates again
    tick2 = FakeTick(bid_price=1620.0, close=1620.0)
    monitor._option_exit_on_tick(tick2)

    assert monitor.peak_premium == 1620.0, f"Expected peak=1620, got {monitor.peak_premium}"


def test_drain_pending_exit_request_calls_exit_paper_position():
    """_drain_pending_option_exit_request consumes request and calls exit_paper_position."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.live_trading = False
    monitor._pending_exit_request = {
        "reason": "PAPER_STOP_LOSS",
        "premium": 1300.0,
        "source": "OPTION_TICK_EXIT",
    }
    monitor.exit_paper_position = MagicMock()
    monitor.exit_live_position = MagicMock()

    monitor._drain_pending_option_exit_request()

    assert monitor._pending_exit_request is None, "Request should be cleared after drain"
    monitor.exit_paper_position.assert_called_once_with(
        "PAPER_STOP_LOSS", 1300.0, "PAPER_STOP_LOSS premium=1300.0 source=OPTION_TICK_EXIT"
    )
    monitor.exit_live_position.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 2026-05-26 Hermes Agent: Options Watchdog tests
# ═══════════════════════════════════════════════════════════════

def test_watchdog_clears_stale_exit_in_progress_when_position_zero():
    """_exit_in_progress=True, position=0 → watchdog clears the flag."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )
    import time

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 0
    monitor.active_side = None
    monitor._exit_in_progress = True
    monitor._exit_start_time = time.monotonic() - 20.0  # >15s ago
    monitor._pending_exit_request = None
    monitor._watchdog_hi_period = 0.0  # force immediate check
    monitor._watchdog_last_hi = 0.0
    monitor._watchdog_last_lo = 0.0
    monitor._watchdog_lo_period = 999.0  # skip Tier 2
    monitor._watchdog_state = "NORMAL"
    monitor.current_option_quote = MagicMock(return_value={"bid": 0.0, "close": 0.0})

    monitor._run_options_watchdog()

    assert monitor._exit_in_progress is False
    assert monitor._exit_start_time == 0.0


def test_watchdog_retries_stuck_exit_with_pending_request():
    """_exit_in_progress=True, pending_exist != None, >15s → retry drain."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )
    import time

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor._exit_in_progress = True
    monitor._exit_start_time = time.monotonic() - 20.0
    monitor._pending_exit_request = {"reason": "PAPER_STOP_LOSS", "premium": 1300.0, "source": "OPTION_TICK_EXIT"}
    monitor._watchdog_hi_period = 0.0
    monitor._watchdog_last_hi = 0.0
    monitor._watchdog_last_lo = 0.0
    monitor._watchdog_lo_period = 999.0
    monitor._watchdog_state = "NORMAL"
    monitor._drain_pending_option_exit_request = MagicMock()
    monitor._log_watchdog_alert = MagicMock()

    monitor._run_options_watchdog()

    monitor._drain_pending_option_exit_request.assert_called_once()
    monitor._log_watchdog_alert.assert_called_once_with(reason="PENDING_EXIT_RETRY", elapsed_secs=__import__("pytest").approx(20.0, rel=0.3))


def test_watchdog_enqueues_retry_when_stuck_without_pending_request():
    """_exit_in_progress=True, pending=None, position>0, >15s → enqueue RETRY_EXIT."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )
    import time

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 1470.0
    monitor._exit_in_progress = True
    monitor._exit_start_time = time.monotonic() - 20.0
    monitor._pending_exit_request = None
    monitor._watchdog_hi_period = 0.0
    monitor._watchdog_last_hi = 0.0
    monitor._watchdog_last_lo = 0.0
    monitor._watchdog_lo_period = 999.0
    monitor._watchdog_state = "NORMAL"
    monitor.current_option_quote = MagicMock(return_value={"bid": 1350.0, "close": 1350.0})
    monitor._log_watchdog_alert = MagicMock()

    monitor._run_options_watchdog()

    assert monitor._pending_exit_request is not None
    assert monitor._pending_exit_request["reason"] == "PAPER_RETRY_EXIT"
    assert monitor._pending_exit_request["premium"] == 1350.0
    assert monitor._pending_exit_request["source"] == "WATCHDOG_RETRY"
    # 2026-06-23 Gemini CLI: Watchdog sets _exit_in_progress = False when enqueueing retry request
    assert monitor._exit_in_progress is False


def test_watchdog_reconciliation_only_warns_no_clear():
    """Reconciliation mismatch → sets RECONCILIATION_MISMATCH state, does NOT clear position."""
    from strategies.options.live_options_squeeze_monitor import (
        ShioajiOptionsSmartMonitor,
    )
    import os, tempfile
    from pathlib import Path

    monitor = ShioajiOptionsSmartMonitor.__new__(ShioajiOptionsSmartMonitor)
    monitor.position = 1
    monitor.active_side = "C"
    monitor._exit_in_progress = False
    monitor._watchdog_hi_period = 0.0
    monitor._watchdog_last_hi = 0.0
    monitor._watchdog_last_lo = -999.0  # force Tier 2
    monitor._watchdog_lo_period = 0.0
    monitor._watchdog_state = "NORMAL"
    # 2026-06-23 Gemini CLI: Initialize _pending_exit_request to prevent AttributeError
    monitor._pending_exit_request = None

    # Create a ledger with only an entry (simulates ledger open = memory matches)
    # To test mismatch, create ledger with only EXIT rows
    ledger_path = Path(tempfile.mktemp(suffix="_test_watchdog_ledger.csv"))
    ledger_path.write_text("trade_id,Timestamp,Mode,Action,Side,Price,Quantity,PnL,Balance,Note\n"
                           "exit_001,2026-05-26 10:00:00,test,PAPER_EXIT,C,1300.0,1,-100,0,exit\n")
    monitor.ledger_path = ledger_path
    monitor._log_watchdog_alert = MagicMock()

    try:
        monitor._run_options_watchdog()

        assert monitor._watchdog_state == "RECONCILIATION_MISMATCH"
        assert monitor.position == 1, "Must NOT clear position on reconciliation mismatch"
        monitor._log_watchdog_alert.assert_called_once()
        call_kwargs = monitor._log_watchdog_alert.call_args[1]
        assert call_kwargs["reason"] == "LEDGER_MEMORY_MISMATCH"
        assert call_kwargs["action"] == "MARK_REVIEW_ONLY"
    finally:
        if ledger_path.exists():
            ledger_path.unlink()
