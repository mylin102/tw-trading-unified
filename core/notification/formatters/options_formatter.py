"""
Options trade notification formatter (TXO-specific).

Builds structured email payloads for options trades with:
  - Position state (side, qty, avg_cost, PnL)
  - Option-specific context (strike, DTE, IV, Greeks)
  - Regime context (mapped to ETF regime names)

Usage (via notifier.py):
    notify_trade_event(event=te, formatter="options", monitor=monitor)
"""

from core.notification.schemas import TradeEvent, RegimeContext, PortfolioSummary
from typing import Any, Optional


# ──────────────────────────────────────────────────────────────
# Options-specific payload
# ──────────────────────────────────────────────────────────────

class OptionsPositionState:
    """Current options position for TXO."""
    def __init__(
        self,
        symbol: str = "TXO",
        side: str = "",
        qty: int = 0,
        avg_cost: float = 0.0,
        last_price: float = 0.0,
        unrealized_pnl: float = 0.0,
        realized_pnl: float = 0.0,
        strike: float = 0.0,
        dte: int = 0,
        point_value: float = 50.0,
    ):
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.avg_cost = avg_cost
        self.last_price = last_price
        self.unrealized_pnl = unrealized_pnl
        self.realized_pnl = realized_pnl
        self.strike = strike
        self.dte = dte
        self.point_value = point_value


class OptionsEmailPayload:
    """Full options notification payload."""
    def __init__(
        self,
        trade_event: TradeEvent,
        position: OptionsPositionState,
        regime: RegimeContext,
        portfolio: Optional[PortfolioSummary] = None,
    ):
        self.trade_event = trade_event
        self.position = position
        self.regime = regime
        self.portfolio = portfolio


# ──────────────────────────────────────────────────────────────
# Regime name map (ETF regime engine compatible)
# ──────────────────────────────────────────────────────────────

_REGIME_MAP = {
    "BULL": "RISK_ON", "BULLISH": "RISK_ON",
    "BEAR": "RISK_OFF", "BEARISH": "RISK_OFF",
    "WEAK": "DEFENSIVE",
    "CHOP": "CHOP", "CHOPPY": "CHOP",
    "TREND": "TRANSITION",
    "RISK_ON": "RISK_ON", "TRANSITION": "TRANSITION",
    "RISK_OFF": "RISK_OFF", "DEFENSIVE": "DEFENSIVE",
}


def _map_regime(raw: str) -> str:
    return _REGIME_MAP.get(raw.strip().upper(), raw or "")


# ──────────────────────────────────────────────────────────────
# BaseFormatter pattern (shared across formatters)
# ──────────────────────────────────────────────────────────────

class OptionsFormatter:
    """Options trade notification formatter.

    Expected context kwargs:
        monitor   — ShioajiOptionsSmartMonitor instance
        position  — OptionsPositionState (optional, built from monitor if absent)
        regime    — RegimeContext (optional, built from monitor if absent)
    """

    def build(self, event: TradeEvent, **context: Any) -> OptionsEmailPayload:
        monitor = context.get("monitor")
        position = context.get("position")
        regime = context.get("regime")

        if position is None and monitor is not None:
            position = self._build_position(monitor, event)
        if regime is None and monitor is not None:
            regime = self._build_regime(monitor)
        if position is None:
            position = OptionsPositionState()
        if regime is None:
            regime = RegimeContext()

        return OptionsEmailPayload(
            trade_event=event,
            position=position,
            regime=regime,
            portfolio=context.get("portfolio"),
        )

    def format_subject(self, payload: OptionsEmailPayload) -> str:
        e = payload.trade_event
        p = payload.position
        is_exit = "EXIT" in e.action

        side_label = {"C": "CALL", "P": "PUT", "THETA": "THETA"}.get(e.side, e.side)
        action_label = _short_action(e.action)

        if is_exit:
            rpl = p.realized_pnl
            return f"[{p.symbol}] {action_label} {side_label} @ {e.price:.1f} | RPL {_format_pnl(rpl)} | {e.trade_id}"
        else:
            return f"[{p.symbol}] {action_label} {side_label} @ {e.price:.1f} | UPL {_format_pnl(p.unrealized_pnl)} | {e.trade_id}"

    def format_body(self, payload: OptionsEmailPayload) -> str:
        lines = []
        lines.extend(_core_section(payload))
        lines.append("")
        lines.extend(_position_section(payload))
        lines.append("")
        lines.extend(_regime_section(payload.regime))
        if payload.portfolio:
            lines.append("")
            lines.extend(_portfolio_section(payload.portfolio))
        return "\n".join(lines)

    # ── Build helpers ──

    def _build_position(self, monitor, event: TradeEvent) -> OptionsPositionState:
        return OptionsPositionState(
            symbol="TXO",
            side=str(getattr(monitor, "active_side", "")),
            qty=int(getattr(monitor, "position", 0)),
            avg_cost=float(getattr(monitor, "entry_price", 0.0)),
            last_price=event.price,
            unrealized_pnl=compute_unrealized_pnl_from_monitor(monitor),
        )

    def _build_regime(self, monitor) -> RegimeContext:
        return RegimeContext(
            regime=str(getattr(monitor, "latest_mid_trend", "")),
            score=float(getattr(monitor, "latest_score", 0.0)),
            action_type="SCOUT",
            momentum=float(getattr(monitor, "latest_score", 0.0)),
            iv=float(getattr(monitor, "latest_iv", 0.0)),
        )


# ──────────────────────────────────────────────────────────────
# Section builders
# ──────────────────────────────────────────────────────────────

def _core_section(payload: OptionsEmailPayload) -> list:
    e = payload.trade_event
    p = payload.position
    r = payload.regime
    is_exit = "EXIT" in e.action

    side_icon = {"C": "🟢", "P": "🔴"}.get(e.side, "⚪")
    pnl_label = "Realized PnL" if is_exit else "Unrealized PnL"
    pnl_value = p.realized_pnl if is_exit else p.unrealized_pnl

    lines = [
        f"{side_icon} {p.symbol} {_short_action(e.action)} {e.side} qty={e.quantity} @ {e.price:.1f}",
        "",
        f"  Position:  {_fmt_signed(p.qty)}{e.side}  (avg cost {p.avg_cost:.1f})",
        f"  {pnl_label}: {_format_pnl(pnl_value)}",
        f"  Last price:     {p.last_price:.1f}",
    ]

    regime_name = _map_regime(r.regime)
    if regime_name:
        lines.append(f"  Regime:  {regime_name} ({r.score:.2f})")
    if r.action_type:
        lines.append(f"  Action:  {r.action_type}")

    return lines


def _position_section(payload: OptionsEmailPayload) -> list:
    p = payload.position
    total_cost = p.avg_cost * p.qty * p.point_value
    market_value = p.last_price * p.qty * p.point_value

    lines = [
        "── Position Detail ──",
        f"  Symbol:       {p.symbol}",
        f"  Side:         {p.side or '-'}",
        f"  Quantity:     {p.qty}",
        f"  Avg Cost:     {p.avg_cost:.1f}",
        f"  Last Price:   {p.last_price:.1f}",
        f"  Entry Cost:   {total_cost:,.0f}",
        f"  Market Value: {market_value:,.0f}",
        f"  Unrealized:   {_format_pnl(p.unrealized_pnl)}",
    ]
    if p.strike:
        lines.append(f"  Strike:       {p.strike:.0f}")
    if p.dte:
        lines.append(f"  DTE:          {p.dte}")
    return lines


def _regime_section(r: RegimeContext) -> list:
    return [
        "── Market Context ──",
        f"  Regime:      {_map_regime(r.regime) or '-'}",
        f"  Score:       {r.score:.1f}" if r.score else "  Score:       -",
        f"  Momentum:    {r.momentum:.1f}" if r.momentum else "  Momentum:    -",
        f"  IV:          {r.iv:.3f}" if r.iv else "  IV:          -",
        f"  Action Type: {r.action_type or '-'}",
    ]


def _portfolio_section(ps: PortfolioSummary) -> list:
    return [
        "── Portfolio Snapshot ──",
        f"  Total Unrealized: {_format_pnl(ps.total_unrealized_pnl)}",
        f"  Total Realized:   {_format_pnl(ps.total_realized_pnl)}",
        f"  Net Exposure:     {ps.total_exposure:,.0f}",
        f"  Net Delta:        {ps.net_delta:+.2f}",
        f"  Net Gamma:        {ps.net_gamma:+.4f}" if ps.net_gamma else "",
        f"  Theta/Day:        {ps.theta_per_day:+.0f}" if ps.theta_per_day else "",
    ]


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _short_action(action: str) -> str:
    if "ENTRY" in action:
        return "ENTRY"
    if "EXIT" in action:
        return "EXIT"
    if "TP1" in action:
        return "TP1"
    if "SCALE" in action or "ADD" in action:
        return "SCALE"
    if "THETA" in action or "THETA_EXIT" in action:
        return "THETA"
    return action


def _format_pnl(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}"


def _fmt_signed(n: int) -> str:
    sign = "+" if n >= 0 else ""
    return f"{sign}{n}"


def compute_unrealized_pnl_from_monitor(monitor) -> float:
    """Compute fee-inclusive unrealized PnL from monitor state."""
    qty = int(getattr(monitor, "position", 0))
    avg_cost = float(getattr(monitor, "entry_price", 0.0))
    # Use the most recent quote as last price
    side = str(getattr(monitor, "active_side", ""))
    if side and hasattr(monitor, "current_option_quote"):
        try:
            quote = monitor.current_option_quote(side)
            last_price = float(quote.get("mid", 0.0) or quote.get("bid", 0.0) or 0.0)
        except Exception:
            last_price = 0.0
    else:
        last_price = 0.0

    if qty == 0 or avg_cost == 0 or last_price == 0:
        return 0.0

    point_value = 50.0
    gross = (last_price - avg_cost) * qty * point_value
    fees = 25.0 * 2 * qty
    tax = (avg_cost + last_price) * point_value * 0.001 * qty
    return round(gross - fees - tax, 0)
