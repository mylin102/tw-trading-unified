"""
StrategyContext — immutable view passed to strategy plugins.

SDD Rule 1 (Single Source of Truth):
    ``StrategyContext`` provides **read-only views** of position and market data.
    Strategies must NEVER mutate these objects.  Position mutations flow
    exclusively through ``PaperTrader.execute_signal()``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PositionView:
    """Read-only snapshot of current position state.

    Strategies read from this; they never write to it.
    """
    size: int = 0                  # +N long, -N short, 0 flat
    entry_price: float = 0.0
    current_stop_loss: float | None = None
    unrealized_pnl: float = 0.0
    has_tp1_hit: bool = False


@dataclass(frozen=True)
class MarketData:
    """Current bar data with all pre-computed indicator columns."""
    last_bar: dict                              # Latest bar as dict
    df_5m: Any | None = None                    # Recent 5m DataFrame
    df_15m: Any | None = None                   # Recent 15m DataFrame
    timestamp: str = ""
    session: int = 0                            # 1=day, 2=night
    regime: str = "NEUTRAL"                     # GSD: Added for Wave 19


@dataclass(frozen=True)
class StrategyContext:
    """Immutable context passed to ``StrategyBase.on_bar()`` each bar.

    Because ``frozen=True``, any mutation attempt raises ``FrozenInstanceError``,
    enforcing SDD Rule 1 at runtime.
    """
    market: MarketData
    position: PositionView
    config: dict                                # This strategy's params
    bar_counter: int = 0                        # Monotonic bar counter
