"""
Exit Manager — unified exit logic for both backtest and live trading.

Mirrors the exit management in FuturesMonitor._strategy_tick():
  1. TP1 partial exit → move SL to breakeven
  2. Trailing stop activation
  3. VWAP reversion exit
  4. Hard stop loss

Used by:
  - strategies/futures/monitor.py (live/paper)
  - scripts/backtest_all_plugins.py (backtest)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExitState:
    """Mutable exit state for an open position. Managed by ExitManager."""
    position: int = 0               # +1 long, -1 short, 0 flat
    entry_price: float = 0.0
    current_sl: float = 0.0         # Current stop loss price
    initial_sl: float = 0.0         # Original stop at entry
    has_tp1_hit: bool = False       # Whether TP1 was triggered
    trailing_activated: bool = False
    vwap_violation_bars: int = 0
    entry_bar: int = 0


@dataclass
class ExitConfig:
    """Config for exit management (loaded from YAML)."""
    tp1_pts: float = 50.0           # Points to trigger TP1
    tp1_lots: int = 1               # Lots to reduce at TP1
    trailing_trigger_pts: float = 100.0  # Points to activate trailing
    trailing_distance_pts: float = 50.0  # Distance for trailing stop
    exit_on_vwap: bool = True
    vwap_confirm_bars: int = 2
    lots_per_trade: int = 1
    max_positions: int = 1


class ExitManager:
    """Unified exit manager — same logic for backtest and live.

    Usage:
        mgr = ExitManager(config)
        # On entry:
        mgr.on_entry(price, sl, bar_counter)
        # On each bar:
        exit_result = mgr.on_bar(price, vwap, bar_counter)
        if exit_result:
            # exit_result.reason is "TP1", "TRAILING", "VWAP", or "STOP_LOSS"
            # exit_result.lots is how many lots to exit
    """

    def __init__(self, config: ExitConfig | None = None):
        self.cfg = config or ExitConfig()
        self.state = ExitState()

    def on_entry(self, price: float, sl: float, bar_counter: int) -> None:
        """Reset state on new entry."""
        self.state.position = 1  # Simplified; caller sets direction
        self.state.entry_price = price
        self.state.current_sl = sl
        self.state.initial_sl = sl
        self.state.has_tp1_hit = False
        self.state.trailing_activated = False
        self.state.vwap_violation_bars = 0
        self.state.entry_bar = bar_counter

    def on_bar(self, price: float, vwap: float, bar_counter: int) -> dict | None:
        """Process exit logic. Returns dict with exit info or None."""
        if self.state.position == 0:
            return None

        pos = self.state.position
        entry = self.state.entry_price
        sl = self.state.current_sl

        # Calculate unrealized PnL in points
        pnl_pts = (price - entry) * pos

        # ── 1. Hard Stop Loss (always checked first) ──────────────────
        if pos > 0 and price <= sl:
            return self._exit_result("STOP_LOSS", price, bar_counter)
        if pos < 0 and price >= sl:
            return self._exit_result("STOP_LOSS", price, bar_counter)

        # ── 2. TP1 Partial Exit ───────────────────────────────────────
        if not self.state.has_tp1_hit and pnl_pts >= self.cfg.tp1_pts:
            self.state.has_tp1_hit = True
            # Move SL to breakeven (+10 pts buffer)
            self.state.current_sl = entry + (10 * pos)
            if self.cfg.tp1_lots < abs(pos):
                return {
                    "action": "PARTIAL_EXIT",
                    "reason": "TP1",
                    "price": price,
                    "lots": self.cfg.tp1_lots,
                    "new_sl": self.state.current_sl,
                    "bars_held": bar_counter - self.state.entry_bar,
                }
            else:
                # Only 1 lot total → full exit at TP1
                return self._exit_result("TP1", price, bar_counter)

        # ── 3. Trailing Stop Activation ──────────────────────────────
        if pnl_pts >= self.cfg.trailing_trigger_pts:
            if not self.state.trailing_activated:
                self.state.trailing_activated = True
            # Trail SL behind price
            trail_sl = price - (self.cfg.trailing_distance_pts * pos)
            if pos > 0:
                self.state.current_sl = max(self.state.current_sl, trail_sl)
            else:
                self.state.current_sl = min(self.state.current_sl, trail_sl)

        # ── 4. VWAP Reversion Exit ───────────────────────────────────
        if self.cfg.exit_on_vwap:
            long_violated = pos > 0 and price < vwap
            short_violated = pos < 0 and price > vwap
            if long_violated or short_violated:
                self.state.vwap_violation_bars += 1
            else:
                self.state.vwap_violation_bars = 0

            if self.state.vwap_violation_bars >= self.cfg.vwap_confirm_bars:
                return self._exit_result("VWAP", price, bar_counter)

        return None

    def _exit_result(self, reason: str, price: float, bar_counter: int) -> dict:
        """Create exit result dict and reset state."""
        result = {
            "action": "EXIT",
            "reason": reason,
            "price": price,
            "lots": abs(self.state.position),
            "bars_held": bar_counter - self.state.entry_bar,
        }
        # Reset
        self.state.position = 0
        self.state.entry_price = 0.0
        self.state.current_sl = 0.0
        return result
