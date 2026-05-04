"""
Futures trade notification formatter (TX/TMF-specific).

Builds structured email payloads for futures trades with:
  - Position state (direction, qty, avg_cost, PnL)
  - Stop loss / ATR context
  - Strategy name and regime
  - Feed freshness and bar age
"""

import datetime
import logging
from typing import Any, Optional

from core.notification.schemas import TradeEvent, RegimeContext, PortfolioSummary

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Futures-specific payload
# ──────────────────────────────────────────────────────────────

class FuturesPositionState:
    """Current futures position."""
    def __init__(
        self,
        symbol: str = "TX",
        side: str = "",            # "LONG" or "SHORT"
        qty: int = 0,
        avg_cost: float = 0.0,
        last_price: float = 0.0,
        unrealized_pnl: float = 0.0,
        realized_pnl: float = 0.0,
        point_value: float = 200.0,  # TX=200, MXF=50, TMF=10
        stop_loss_pts: float = 0.0,
        atr: float = 0.0,
        contract_month: str = "",
        strategy_name: str = "",
    ):
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.avg_cost = avg_cost
        self.last_price = last_price
        self.unrealized_pnl = unrealized_pnl
        self.realized_pnl = realized_pnl
        self.point_value = point_value
        self.stop_loss_pts = stop_loss_pts
        self.atr = atr
        self.contract_month = contract_month
        self.strategy_name = strategy_name


class FuturesFeedState:
    """Data freshness for futures feed."""
    def __init__(
        self,
        feed_age_secs: float = 0.0,
        bar_age_secs: float = 0.0,
        last_tick_ts: str = "",
        last_bar_ts: str = "",
    ):
        self.feed_age_secs = feed_age_secs
        self.bar_age_secs = bar_age_secs
        self.last_tick_ts = last_tick_ts
        self.last_bar_ts = last_bar_ts


class FuturesEmailPayload:
    """Full futures notification payload."""
    def __init__(
        self,
        trade_event: TradeEvent,
        position: FuturesPositionState,
        regime: RegimeContext,
        feed: Optional[FuturesFeedState] = None,
        portfolio: Optional[PortfolioSummary] = None,
    ):
        self.trade_event = trade_event
        self.position = position
        self.regime = regime
        self.feed = feed
        self.portfolio = portfolio


# ──────────────────────────────────────────────────────────────
# Regime name map (same as options, shared via core/notification later)
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
# Formatter
# ──────────────────────────────────────────────────────────────

class FuturesFormatter:
    """Futures trade notification formatter.

    Expected context kwargs:
        monitor    — FuturesMonitor instance
        position   — FuturesPositionState (optional, built from monitor if absent)
        regime     — RegimeContext (optional, built from monitor if absent)
        feed       — FuturesFeedState (optional, built from monitor if absent)
        realized_pnl — float, for EXIT events
    """

    def build(self, event: TradeEvent, **context: Any) -> FuturesEmailPayload:
        monitor = context.get("monitor")
        position = context.get("position")
        regime = context.get("regime")
        feed = context.get("feed")

        if position is None and monitor is not None:
            position = self._build_position(monitor, event)
        if regime is None and monitor is not None:
            regime = self._build_regime(monitor)
        if feed is None and monitor is not None:
            feed = self._build_feed(monitor)
        if position is None:
            position = FuturesPositionState()
        if regime is None:
            regime = RegimeContext()

        # For EXIT, set realized PnL from context
        if "EXIT" in event.action:
            rpl = context.get("realized_pnl", 0.0)
            position.realized_pnl = float(rpl)

        return FuturesEmailPayload(
            trade_event=event,
            position=position,
            regime=regime,
            feed=feed,
            portfolio=context.get("portfolio"),
        )

    def format_subject(self, payload: FuturesEmailPayload) -> str:
        e = payload.trade_event
        p = payload.position
        is_exit = "EXIT" in e.action

        action_label = _short_action(e.action)
        side_label = e.side

        if is_exit:
            rpl = p.realized_pnl
            return f"[{p.symbol}] {action_label} {side_label} @ {e.price:.0f} | RPL {_format_pnl(rpl)} | {e.trade_id}"
        else:
            return f"[{p.symbol}] {action_label} {side_label} @ {e.price:.0f} | UPL {_format_pnl(p.unrealized_pnl)} | {e.trade_id}"

    def format_body(self, payload: FuturesEmailPayload) -> str:
        lines = []
        lines.extend(_core_section(payload))
        lines.append("")
        lines.extend(_position_section(payload))
        lines.append("")
        lines.extend(_regime_section(payload.regime))
        if payload.feed:
            lines.append("")
            lines.extend(_feed_section(payload.feed))
        if payload.portfolio:
            lines.append("")
            lines.extend(_portfolio_section(payload.portfolio))
        return "\n".join(lines)

    # ── Build helpers ──

    def _build_position(self, monitor, event: TradeEvent) -> FuturesPositionState:
        trader = getattr(monitor, "trader", None)
        pos = getattr(trader, "position", 0) if trader else 0
        entry_price = getattr(trader, "entry_price", 0.0) if trader else 0.0
        direction = getattr(trader, "direction", "") if trader else ""

        # Stop loss from monitor or trader
        sl_pts = 0.0
        if trader and hasattr(trader, "current_stop_loss"):
            sl_pts = float(getattr(trader, "current_stop_loss", 0.0) or 0.0)
        elif hasattr(monitor, "RISK"):
            sl_pts = float(monitor.RISK.get("stop_loss_pts", 0))

        return FuturesPositionState(
            symbol="MXF",
            side=direction,
            qty=int(pos),
            avg_cost=float(entry_price),
            last_price=event.price,
            unrealized_pnl=0.0,  # caller computes
            point_value=float(getattr(trader, "point_value", 50.0)) if trader else 50.0,
            stop_loss_pts=sl_pts,
            atr=float(monitor._entry_features_futures.get("atr", 0.0)) if hasattr(monitor, "_entry_features_futures") and monitor._entry_features_futures else 0.0,
            strategy_name=str(getattr(monitor, "active_strategy_name", "")),
            contract_month=str(getattr(monitor, "contract_month", "")),
        )

    def _build_regime(self, monitor) -> RegimeContext:
        ctx = getattr(monitor, "_entry_features_futures", {}) or {}
        return RegimeContext(
            regime=ctx.get("regime", ""),
            score=float(ctx.get("score", 0.0)),
            action_type="",
            momentum=float(ctx.get("momentum", 0.0)),
        )

    def _build_feed(self, monitor) -> FuturesFeedState:
        now = time.time()
        last_tick = float(getattr(monitor, "last_tick_at", now))
        feed_age = now - last_tick if last_tick > 0 else 0.0

        # Bar age from _last_bar_ts
        bar_ts = int(getattr(monitor, "_last_bar_ts", int(now / 300) * 300))
        bar_age = now - bar_ts if bar_ts > 0 else 0.0

        return FuturesFeedState(
            feed_age_secs=feed_age,
            bar_age_secs=bar_age,
            last_tick_ts=datetime.datetime.fromtimestamp(last_tick).strftime("%H:%M:%S") if last_tick > 0 else "",
            last_bar_ts=datetime.datetime.fromtimestamp(bar_ts).strftime("%H:%M:%S") if bar_ts > 0 else "",
        )


# ──────────────────────────────────────────────────────────────
# Section builders
# ──────────────────────────────────────────────────────────────

def _core_section(payload: FuturesEmailPayload) -> list:
    e = payload.trade_event
    p = payload.position
    r = payload.regime
    is_exit = "EXIT" in e.action

    icon = "🟢" if p.side in ("LONG", "BUY") else ("🔴" if p.side in ("SHORT", "SELL") else "⚪")
    pnl_label = "Realized PnL" if is_exit else "Unrealized PnL"
    pnl_value = p.realized_pnl if is_exit else p.unrealized_pnl

    lines = [
        f"{icon} {p.symbol} {_short_action(e.action)} {e.side} qty={e.quantity} @ {e.price:.0f}",
        "",
        f"  Position:  {_fmt_signed(p.qty)} {p.symbol} ({p.side})  (avg cost {p.avg_cost:.0f})",
        f"  {pnl_label}: {_format_pnl(pnl_value)}",
    ]

    # Stop loss info
    if p.stop_loss_pts and p.qty > 0:
        if p.side in ("LONG", "BUY"):
            sl_price = p.avg_cost - p.stop_loss_pts
        else:
            sl_price = p.avg_cost + p.stop_loss_pts
        lines.append(f"  Stop Loss:  {sl_price:.0f} ({p.stop_loss_pts:.0f} pts)")
    if p.atr:
        lines.append(f"  ATR:        {p.atr:.1f}")

    if p.strategy_name:
        lines.append(f"  Strategy:   {p.strategy_name}")

    regime_name = _map_regime(r.regime)
    if regime_name:
        lines.append(f"  Regime:     {regime_name} ({r.score:.1f})")

    return lines


def _position_section(payload: FuturesEmailPayload) -> list:
    p = payload.position
    total_cost = p.avg_cost * p.qty * p.point_value
    market_value = p.last_price * p.qty * p.point_value

    contract_label = f"{p.contract_month}" if p.contract_month else ""

    lines = [
        "── Position Detail ──",
        f"  Symbol:       {p.symbol}",
        f"  Side:         {p.side or '-'}",
        f"  Quantity:     {p.qty}",
        f"  Avg Cost:     {p.avg_cost:.0f}",
        f"  Last Price:   {p.last_price:.0f}",
        f"  Entry Value:  {total_cost:,.0f}",
        f"  Market Value: {market_value:,.0f}",
        f"  Unrealized:   {_format_pnl(p.unrealized_pnl)}",
    ]
    if contract_label:
        lines.append(f"  Contract:     {contract_label}")
    if p.atr:
        lines.append(f"  ATR:          {p.atr:.1f}")
    if p.stop_loss_pts and p.qty > 0:
        lines.append(f"  Stop Loss:    {p.stop_loss_pts:.0f} pts")
    if p.strategy_name:
        lines.append(f"  Strategy:     {p.strategy_name}")
    return lines


def _regime_section(r: RegimeContext) -> list:
    return [
        "── Market Context ──",
        f"  Regime:      {_map_regime(r.regime) or '-'}",
        f"  Score:       {r.score:.1f}" if r.score else "  Score:       -",
        f"  Momentum:    {r.momentum:.1f}" if r.momentum else "  Momentum:    -",
        f"  Action Type: {r.action_type or '-'}",
    ]


def _feed_section(f: FuturesFeedState) -> list:
    feed_status = "✅ Fresh"
    feed_color = ""
    if f.feed_age_secs > 600:
        feed_status = "🔴 CRITICAL"
    elif f.feed_age_secs > 120:
        feed_status = "🟡 Stale"

    bar_status = "✅ Fresh"
    if f.bar_age_secs > 600:
        bar_status = "🔴 Stale"
    elif f.bar_age_secs > 120:
        bar_status = "🟡 Aging"

    return [
        "── Feed Health ──",
        f"  Tick Feed:  {feed_status} ({f.feed_age_secs:.0f}s ago @ {f.last_tick_ts})",
        f"  Bar Data:   {bar_status} ({f.bar_age_secs:.0f}s ago @ {f.last_bar_ts})",
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

import time  # needed for _build_feed


def _short_action(action: str) -> str:
    if "ENTRY" in action:
        return "ENTRY"
    if "EXIT" in action:
        return "EXIT"
    if "TP1" in action:
        return "TP1"
    if "SCALE" in action or "ADD" in action:
        return "SCALE"
    return "TRADE"


def _format_pnl(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}"


def _fmt_signed(n: int) -> str:
    sign = "+" if n >= 0 else ""
    return f"{sign}{n}"


def compute_futures_pnl(position: FuturesPositionState) -> float:
    """Compute fee-inclusive unrealized PnL for futures position."""
    if position.qty == 0 or position.avg_cost == 0 or position.last_price == 0:
        return 0.0

    if position.side in ("LONG", "BUY"):
        gross = (position.last_price - position.avg_cost) * position.qty * position.point_value
    else:
        gross = (position.avg_cost - position.last_price) * position.qty * position.point_value

    fees = 25.0 * 2 * position.qty
    tax_rate = 0.00002  # 期貨交易稅 ~0.002%
    tax = (position.avg_cost + position.last_price) * position.point_value * tax_rate * position.qty
    return round(gross - fees - tax, 0)
