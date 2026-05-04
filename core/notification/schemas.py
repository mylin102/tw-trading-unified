"""
Shared data contracts for trade notifications.

These are asset-class-agnostic. Formatters can subclass or compose
them for asset-specific fields (e.g. OptionsPositionState adds strike,
FuturesPositionState adds contract month).
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeEvent:
    """What just happened."""
    trade_id: str                     # trade_20260504_ab12cd34
    action: str                       # "LIVE_ENTRY_FILLED", "LIVE_EXIT_FILLED", etc.
    side: str                         # "C", "P", "LONG", "SHORT"
    price: float
    quantity: int


@dataclass
class RegimeContext:
    """Market regime at notification time."""
    regime: str = ""                  # RISK_ON, TRANSITION, DEFENSIVE, RISK_OFF, CHOP
    score: float = 0.0
    action_type: str = ""             # SCOUT, SCALE, EXIT
    momentum: float = 0.0
    iv: float = 0.0


@dataclass
class PnLSnapshot:
    """PnL state for one position."""
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class PositionSnapshot:
    """Current position state for one symbol."""
    symbol: str
    qty: int
    avg_cost: float
    last_price: float
    pnl: PnLSnapshot = field(default_factory=PnLSnapshot)


@dataclass
class PortfolioSummary:
    """Aggregate across all positions."""
    total_unrealized_pnl: float = 0.0
    total_realized_pnl: float = 0.0
    total_exposure: float = 0.0
    net_delta: float = 0.0
    net_gamma: float = 0.0
    theta_per_day: float = 0.0
