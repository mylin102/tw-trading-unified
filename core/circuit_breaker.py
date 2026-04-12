"""
Circuit Breaker — automatic risk control for adaptive trading.

Three-level degradation:
  Level 1: consecutive_losses >= 3 → DIAGNOSE (not blind switch)
  Level 2: session PnL < daily_loss_cap → HALT
  Level 3: rolling losses trigger full paper-mode switch

Two independent instances required: one for day, one for night.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from enum import Enum


class Action(str, Enum):
    CONTINUE = "CONTINUE"
    DIAGNOSE = "DIAGNOSE"    # 3+ consecutive losses → run root cause analysis
    HALT = "HALT"            # Daily loss cap breached → stop trading
    REDUCE_SIZE = "REDUCE_SIZE"  # Reduce to 1 lot


@dataclass
class BreakerState:
    """Mutable state for one session's circuit breaker."""
    consecutive_losses: int = 0
    session_pnl: float = 0.0
    daily_loss_cap: float = 5000.0    # 5% of 100k capital
    max_consecutive: int = 3          # Threshold for DIAGNOSE
    halted: bool = False
    halt_date: str | None = None      # Date when halted


class CircuitBreaker:
    """
    Per-session circuit breaker.

    Usage:
        day_breaker = CircuitBreaker(session="day", daily_loss_cap=5000)
        night_breaker = CircuitBreaker(session="night", daily_loss_cap=5000)

        action = day_breaker.check(pnl=-1200, consecutive_losses=3)
        # → Action.DIAGNOSE
    """

    def __init__(
        self,
        session: str = "day",
        daily_loss_cap: float = 5000.0,
        max_consecutive: int = 3,
    ):
        self.session = session
        self.daily_loss_cap = daily_loss_cap
        self.max_consecutive = max_consecutive
        self._state = BreakerState(
            daily_loss_cap=daily_loss_cap,
            max_consecutive=max_consecutive,
        )

    def check(
        self,
        pnl: float = 0.0,
        consecutive_losses: int = 0,
    ) -> Action:
        """
        Evaluate circuit breaker state.

        Args:
            pnl: current session PnL (negative = loss)
            consecutive_losses: number of consecutive losing trades

        Returns:
            Action: CONTINUE, DIAGNOSE, HALT, or REDUCE_SIZE
        """
        # Check if halted from previous session
        if self._state.halted:
            # Halt resets next day
            today = date.today().isoformat()
            if self._state.halt_date != today:
                self._state.halted = False
                self._state.session_pnl = 0.0
                self._state.consecutive_losses = 0
                self._state.halt_date = None
            else:
                return Action.HALT

        # Update state
        self._state.session_pnl += pnl
        self._state.consecutive_losses = consecutive_losses

        # Level 1: consecutive losses → diagnose
        if consecutive_losses >= self.max_consecutive:
            return Action.DIAGNOSE

        # Level 2: daily loss cap → halt
        if self._state.session_pnl <= -self.daily_loss_cap:
            self._state.halted = True
            self._state.halt_date = date.today().isoformat()
            return Action.HALT

        # Level 3: partial mitigation (1-2% daily loss)
        if self._state.session_pnl <= -(self.daily_loss_cap * 0.4):
            return Action.REDUCE_SIZE

        return Action.CONTINUE

    def reset(self):
        """Reset circuit breaker (called at start of new session)."""
        self._state.session_pnl = 0.0
        self._state.consecutive_losses = 0
        self._state.halted = False
        self._state.halt_date = None

    @property
    def is_halted(self) -> bool:
        """Check if the circuit breaker is currently halted."""
        if not self._state.halted:
            return False
        # Check if halt should expire
        today = date.today().isoformat()
        if self._state.halt_date != today:
            self._state.halted = False
            self._state.session_pnl = 0.0
            self._state.consecutive_losses = 0
            self._state.halt_date = None
            return False
        return True

    @property
    def state(self) -> BreakerState:
        """Read-only snapshot of current state."""
        return self._state
