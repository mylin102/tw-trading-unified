"""
Futures trade notification formatter (TX/TMF-specific).

Placeholder — structure matches OptionsFormatter so the notifier dispatch works.
Implement when futures notifications are needed.

Expected additions:
  - Contract month (near/far)
  - Point-based PnL (vs premium-based for options)
  - Leverage / margin context
  - Stop loss / ATR
"""

from core.notification.schemas import TradeEvent, RegimeContext, PortfolioSummary
from typing import Any, Optional


class FuturesPositionState:
    def __init__(
        self,
        symbol: str = "TX",
        side: str = "",
        qty: int = 0,
        avg_cost: float = 0.0,
        last_price: float = 0.0,
        unrealized_pnl: float = 0.0,
        realized_pnl: float = 0.0,
        point_value: float = 200.0,
        contract_month: str = "",
    ):
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.avg_cost = avg_cost
        self.last_price = last_price
        self.unrealized_pnl = unrealized_pnl
        self.realized_pnl = realized_pnl
        self.point_value = point_value
        self.contract_month = contract_month


class FuturesEmailPayload:
    def __init__(
        self,
        trade_event: TradeEvent,
        position: FuturesPositionState,
        regime: RegimeContext,
        portfolio: Optional[PortfolioSummary] = None,
    ):
        self.trade_event = trade_event
        self.position = position
        self.regime = regime
        self.portfolio = portfolio


class FuturesFormatter:
    """Futures trade notification formatter.

    NOTE: This is a stub. The structure mirrors OptionsFormatter so
    notify_trade_event(formatter="futures") resolves correctly.
    """

    def build(self, event: TradeEvent, **context: Any) -> FuturesEmailPayload:
        return FuturesEmailPayload(
            trade_event=event,
            position=FuturesPositionState(),
            regime=RegimeContext(),
        )

    def format_subject(self, payload: FuturesEmailPayload) -> str:
        e = payload.trade_event
        return f"[{payload.position.symbol}] {e.action} {e.side} @ {e.price:.1f} | {e.trade_id}"

    def format_body(self, payload: FuturesEmailPayload) -> str:
        return f"[{payload.position.symbol}] {payload.trade_event.action} — formatter TBD"
