import math

import pandas as pd


def _safe_mean(values):
    return sum(values) / len(values) if values else 0.0


def _max_consecutive(trades, predicate):
    streak = 0
    best = 0
    for trade in trades:
        if predicate(trade):
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def compute_equity_metrics(equity_curve, initial_balance, bars_per_year=252 * 54):
    equity = pd.Series(equity_curve, dtype=float)
    if equity.empty:
        return {
            "total_return": 0.0,
            "annual_return": 0.0,
            "annual_volatility": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "final_equity": initial_balance,
        }

    returns = equity.pct_change().dropna()
    total_return = (equity.iloc[-1] / initial_balance) - 1 if initial_balance else 0.0
    years = max(len(equity) / bars_per_year, 1 / bars_per_year)
    annual_return = ((equity.iloc[-1] / initial_balance) ** (1 / years) - 1) if initial_balance and equity.iloc[-1] > 0 else 0.0
    annual_volatility = returns.std() * math.sqrt(bars_per_year) if not returns.empty else 0.0
    sharpe = annual_return / annual_volatility if annual_volatility else 0.0
    max_drawdown = ((equity - equity.cummax()) / equity.cummax()).min()

    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "annual_volatility": annual_volatility,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "final_equity": equity.iloc[-1],
    }


def compute_trade_metrics(trades):
    if not trades:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
        }

    pnls = [float(trade.get("pnl", 0.0)) for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    avg_win = _safe_mean(wins)
    avg_loss = _safe_mean(losses)
    loss_abs = abs(avg_loss) if avg_loss else 0.0

    return {
        "trade_count": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_pnl": _safe_mean(pnls),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_loss_ratio": (avg_win / loss_abs) if loss_abs else 0.0,
        "max_consecutive_wins": _max_consecutive(trades, lambda trade: trade.get("pnl", 0.0) > 0),
        "max_consecutive_losses": _max_consecutive(trades, lambda trade: trade.get("pnl", 0.0) < 0),
    }


def summarize_backtest(equity_curve, trades, initial_balance, bars_per_year=252 * 54):
    summary = {}
    summary.update(compute_equity_metrics(equity_curve, initial_balance, bars_per_year=bars_per_year))
    summary.update(compute_trade_metrics(trades))
    summary["net_profit"] = summary["final_equity"] - initial_balance
    return summary
