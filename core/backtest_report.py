"""
Backtest metrics and reporting — integrates with the Pluggable Strategy Module System.

Provides:
- TradeRecord dataclass for standardized trade logging.
- calculate_backtest_metrics() for core performance metrics (Expectancy, PF, DD, etc.).
- generate_detailed_report() for human-readable Markdown output.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

from core.signal import Signal


@dataclass
class TradeRecord:
    """Single trade record. Matches Signal output and Monitor execution results.

    Created by PaperTrader or backtest engine after a trade is fully executed.
    """
    trade_id: int
    strategy_name: str
    action: str                     # BUY / SELL (entry) or EXIT / PARTIAL_EXIT
    entry_price: float
    exit_price: float
    pnl_points: float               # Raw points moved
    pnl_dollars: float              # Cash PnL after fees/taxes
    risk_r: float = 1.0             # Risk units (1.0 = hit stop loss)
    bars_held: int = 0
    reason: str = ""
    entry_time: str = ""
    exit_time: str = ""
    confidence: float = 1.0
    signal: Optional[Signal] = field(default=None, repr=False)  # Original signal that triggered entry


def calculate_backtest_metrics(
    trades: List[TradeRecord],
    initial_balance: float = 100_000.0,
) -> Dict[str, Any]:
    """Calculate all core performance metrics including Expectancy.

    Returns a dict suitable for dashboard rendering or console output.
    """
    if not trades:
        return {"error": "No trades recorded"}

    total_trades = len(trades)
    total_pnl = sum(t.pnl_dollars for t in trades)
    winners = [t for t in trades if t.pnl_dollars > 0]
    losers = [t for t in trades if t.pnl_dollars <= 0]

    win_rate = len(winners) / total_trades if total_trades else 0.0
    avg_win = sum(t.pnl_dollars for t in winners) / len(winners) if winners else 0.0
    avg_loss = abs(sum(t.pnl_dollars for t in losers) / len(losers)) if losers else 0.0

    gross_profit = sum(t.pnl_dollars for t in winners)
    gross_loss = abs(sum(t.pnl_dollars for t in losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # ── Expectancy (USD) ──────────────────────────────────────────────
    expectancy_usd = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    # ── Expectancy (R-multiples) ──────────────────────────────────────
    r_vals = [t.pnl_dollars / t.risk_r for t in trades if t.risk_r > 0]
    if r_vals:
        pos_r = [r for r in r_vals if r > 0]
        neg_r = [abs(r) for r in r_vals if r <= 0]
        avg_win_r = sum(pos_r) / len(pos_r) if pos_r else 0.0
        avg_loss_r = sum(neg_r) / len(neg_r) if neg_r else 0.0
        expectancy_r = (win_rate * avg_win_r) - ((1 - win_rate) * avg_loss_r)
    else:
        expectancy_r = 0.0
        avg_win_r = avg_loss_r = 0.0

    # ── Equity Curve & Max Drawdown ───────────────────────────────────
    pnl_series = np.array([t.pnl_dollars for t in trades])
    equity = initial_balance + np.cumsum(pnl_series)
    equity = np.insert(equity, 0, initial_balance)
    peak = np.maximum.accumulate(equity)
    drawdown = equity - peak
    max_dd_idx = np.argmin(drawdown)
    max_dd_usd = drawdown[max_dd_idx]
    peak_at_dd = peak[max_dd_idx]
    max_dd_pct = (max_dd_usd / peak_at_dd * 100) if peak_at_dd != 0 else 0.0

    # ── Streaks & Duration ────────────────────────────────────────────
    max_loss_streak = 0
    current_streak = 0
    for t in trades:
        if t.pnl_dollars <= 0:
            current_streak += 1
            max_loss_streak = max(max_loss_streak, current_streak)
        else:
            current_streak = 0

    avg_bars = sum(t.bars_held for t in trades) / total_trades if total_trades else 0.0

    return {
        "total_trades": total_trades,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate * 100, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else "∞",
        "expectancy_usd": round(expectancy_usd, 2),
        "expectancy_r": round(expectancy_r, 3),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_win_r": round(avg_win_r, 3),
        "avg_loss_r": round(avg_loss_r, 3),
        "max_dd_usd": round(max_dd_usd, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "max_loss_streak": max_loss_streak,
        "avg_bars_held": round(avg_bars, 1),
        "initial_balance": initial_balance,
        "final_balance": round(initial_balance + total_pnl, 2),
    }


def generate_detailed_report(
    metrics: Dict[str, Any],
    strategy_name: str = "Unnamed",
    expected_pf: float = 0.0,
    expected_wr: float = 0.0,
) -> str:
    """Generate a formatted Markdown report for console or dashboard."""
    if "error" in metrics:
        return "❌ No trades recorded."

    # Quality badges
    pf_val = metrics["profit_factor"]
    wr_val = metrics["win_rate"]
    dd_val = metrics["max_dd_pct"]
    ex_r = metrics["expectancy_r"]

    pf_badge = "⭐ Excellent" if pf_val >= 1.8 else "✅ Good" if pf_val >= 1.5 else "⚠️ Marginal" if pf_val >= 1.0 else "❌ Failing"
    wr_badge = "⭐ High" if wr_val >= 50 else "✅ OK" if wr_val >= 40 else "⚠️ Low"
    dd_badge = "✅ Safe" if abs(dd_val) < 15 else "⚠️ Elevated" if abs(dd_val) < 25 else "❌ Dangerous"

    verdict = (
        "✅ Strategy is ready for paper trading."
        if ex_r > 0.5 and pf_val >= 1.5 and abs(dd_val) < 20
        else "⚠️ Needs optimization (low expectancy or high DD)."
    )

    comparison = ""
    if expected_pf > 0:
        diff = pf_val - expected_pf
        comparison += f"\n- PF vs Expected: {pf_val:.2f} vs {expected_pf:.2f} ({diff:+.2f})"
    if expected_wr > 0:
        diff = wr_val - expected_wr
        comparison += f"\n- WR vs Expected: {wr_val:.1f}% vs {expected_wr:.1f}% ({diff:+.1f}%)"

    return f"""
# 📊 {strategy_name} Backtest Report
**Trades**: {metrics['total_trades']} | **Start**: ${metrics['initial_balance']:,.0f} → **End**: ${metrics['final_balance']:,.0f}

### Key Metrics
| Metric | Value | Assessment |
|---|---|---|
| Profit Factor | {metrics['profit_factor']} | {pf_badge} |
| Win Rate | {wr_val}% | {wr_badge} |
| Expectancy (R) | {metrics['expectancy_r']:.3f} R | {'⭐ Positive' if metrics['expectancy_r'] > 0 else '❌ Negative'} |
| Max Drawdown | {dd_val}% (${metrics['max_dd_usd']:,.0f}) | {dd_badge} |
| Avg Win / Loss | ${metrics['avg_win']:,.0f} / -${metrics['avg_loss']:,.0f} | Ratio: {metrics['avg_win']/metrics['avg_loss']:.2f}x |
| Max Loss Streak | {metrics['max_loss_streak']} trades | {'⚠️ Long' if metrics['max_loss_streak'] >= 8 else '✅ Normal'} |
{comparison}

### Verdict
{verdict}
---
*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
""".strip()
