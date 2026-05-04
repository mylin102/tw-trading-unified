"""
Trade notification formatter — structured, layered email payloads.

Production principle:
  Notification is not source of truth.
  Ledger persistence is source of truth.

Every notification carries enough context for immediate decision:
  1. What happened (trade_id, action, price)
  2. Current position state (qty, avg_cost, unrealized_pnl)
  3. Regime context (regime, score, action type)
  4. Portfolio summary (total PnL, exposure)

Layered delivery:
  Layer 1 (always visible): core — position + PnL + regime
  Layer 2 (collapsible details): full portfolio snapshot + risk metrics
"""

from dataclasses import dataclass, field
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Data contracts — what the formatter needs
# ──────────────────────────────────────────────────────────────

@dataclass
class PositionState:
    """Current position for one symbol."""
    symbol: str                       # e.g. "TXO"
    side: str                         # "C" or "P" or None
    qty: int                          # total position size
    avg_cost: float                   # average entry price
    last_price: float                 # current market price
    unrealized_pnl: float             # (last - cost) * qty_point_value - fees
    point_value: float = 50.0         # TXO contract multiplier
    entry_price: float = 0.0          # latest entry price (may differ from avg_cost)


@dataclass
class PortfolioSummary:
    """Aggregate across all positions."""
    total_unrealized_pnl: float = 0.0
    total_realized_pnl: float = 0.0
    total_exposure: float = 0.0       # premium at risk
    net_delta: float = 0.0
    net_gamma: float = 0.0
    theta_per_day: float = 0.0


@dataclass
class RegimeContext:
    """Market regime at notification time."""
    regime: str = ""                  # "BULL", "BEAR", "TRANSITION", etc.
    score: float = 0.0
    action_type: str = ""             # "SCOUT", "SCALE", "EXIT"
    momentum: float = 0.0
    iv: float = 0.0


@dataclass
class TradeEvent:
    """What just happened."""
    trade_id: str                     # trade_20260504_ab12cd34
    action: str                       # "LIVE_ENTRY_FILLED", "LIVE_EXIT_FILLED", etc.
    side: str                         # "C" or "P"
    price: float
    quantity: int


# ──────────────────────────────────────────────────────────────
# Build payload (what the notification carries)
# ──────────────────────────────────────────────────────────────

@dataclass
class EmailPayload:
    trade_event: TradeEvent
    position: PositionState
    regime: RegimeContext
    portfolio: Optional[PortfolioSummary] = None


# ──────────────────────────────────────────────────────────────
# Formatting — subject + body (layered)
# ──────────────────────────────────────────────────────────────

def format_subject(payload: EmailPayload) -> str:
    e = payload.trade_event
    p = payload.position
    r = payload.regime

    # [TXO] ENTRY CALL @ 10.0 | trade_20260504_ab12cd34
    side_label = {"C": "CALL", "P": "PUT", "THETA": "THETA"}.get(e.side, e.side)
    action_label = _short_action(e.action)

    return f"[{p.symbol}] {action_label} {side_label} @ {e.price:.1f} | {e.trade_id}"


def format_body(payload: EmailPayload) -> str:
    """Return plain-text layered body."""
    lines = []

    # ── Layer 1: Core (always visible) ──
    lines.extend(_core_section(payload))
    lines.append("")

    # ── Layer 2: Position detail ──
    lines.extend(_position_section(payload))
    lines.append("")

    # ── Layer 3: Regime context ──
    lines.extend(_regime_section(payload))

    # ── Layer 4: Portfolio (only if available) ──
    if payload.portfolio:
        lines.append("")
        lines.extend(_portfolio_section(payload.portfolio))

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Section builders
# ──────────────────────────────────────────────────────────────

def _core_section(payload: EmailPayload) -> list:
    e = payload.trade_event
    p = payload.position
    r = payload.regime

    side_icon = {"C": "🟢", "P": "🔴"}.get(e.side, "⚪")
    action_label = {"ENTRY": "ENTRY", "EXIT": "EXIT", "SCALE": "ADD"}.get(
        _short_action(e.action), _short_action(e.action)
    )

    pnl_str = _format_pnl(p.unrealized_pnl)
    lines = [
        f"{side_icon} {p.symbol} {action_label} {e.side} qty={e.quantity} @ {e.price:.1f}",
        "",
        f"  Position:  {_fmt_signed(p.qty)}{e.side}  (avg cost {p.avg_cost:.1f})",
        f"  Unrealized PnL: {pnl_str}",
        f"  Last price:     {p.last_price:.1f}",
    ]

    if r.regime:
        lines.append(f"  Regime:  {r.regime} ({r.score:.2f})")
    if r.action_type:
        lines.append(f"  Action:  {r.action_type}")

    return lines


def _position_section(payload: EmailPayload) -> list:
    e = payload.trade_event
    p = payload.position
    total_cost = p.avg_cost * p.qty * p.point_value
    market_value = p.last_price * p.qty * p.point_value

    return [
        "── Position Detail ──",
        f"  Symbol:       {p.symbol}",
        f"  Side:         {e.side or '-'}",
        f"  Quantity:     {p.qty}",
        f"  Avg Cost:     {p.avg_cost:.1f}",
        f"  Last Price:   {p.last_price:.1f}",
        f"  Entry Cost:   {total_cost:,.0f}",
        f"  Market Value: {market_value:,.0f}",
        f"  Unrealized:   {_format_pnl(p.unrealized_pnl)}",
    ]


def _regime_section(payload: EmailPayload) -> list:
    r = payload.regime
    return [
        "── Market Context ──",
        f"  Regime:      {r.regime or '-'}",
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
    """Map log_trade action to short label."""
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
    """+$1,234 or -$567."""
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}"


def _fmt_signed(n: int) -> str:
    """+3 or -1."""
    sign = "+" if n >= 0 else ""
    return f"{sign}{n}"


# ──────────────────────────────────────────────────────────────
# Convenience builders for callers
# ──────────────────────────────────────────────────────────────

def build_from_monitor(monitor, trade_event: TradeEvent) -> EmailPayload:
    """Build a full EmailPayload from a ShioajiOptionsSmartMonitor instance.

    This is the primary entry point for live_options_squeeze_monitor.py.
    """
    pos = PositionState(
        symbol="TXO",
        side=getattr(monitor, "active_side", ""),
        qty=int(getattr(monitor, "position", 0)),
        avg_cost=float(getattr(monitor, "entry_price", 0.0)),
        last_price=trade_event.price,  # latest known price
        unrealized_pnl=0.0,  # caller should compute after construction
    )

    regime = RegimeContext(
        regime=str(getattr(monitor, "latest_mid_trend", "")),
        score=float(getattr(monitor, "latest_score", 0.0)),
        action_type="SCOUT",  # caller to override if SCALE
        momentum=float(getattr(monitor, "latest_score", 0.0)),
        iv=float(getattr(monitor, "latest_iv", 0.0)),
    )

    return EmailPayload(
        trade_event=trade_event,
        position=pos,
        regime=regime,
        portfolio=None,  # populated separately if needed
    )


def compute_unrealized_pnl(pos: PositionState) -> float:
    """Compute unrealized PnL from position state, fees included.

    Formula:
      gross = (last_price - avg_cost) * qty * point_value
      fees  = broker_fee_per_side * 2 * qty
      tax   = (entry_price + last_price) * point_value * tax_rate * qty
      net   = gross - fees - tax
    """
    gross = (pos.last_price - pos.avg_cost) * pos.qty * pos.point_value
    # Fees: ~20 broker + ~5 exchange per side, 2 sides
    fees = 25.0 * 2 * pos.qty
    tax = (pos.avg_cost + pos.last_price) * pos.point_value * 0.001 * pos.qty
    return round(gross - fees - tax, 0)
