import logging
import pandas as pd
from typing import Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)

class PerformanceAggregator:
    """
    Aggregates trading metrics from the SQLite SSOT (Single Source of Truth).
    Provides L4 verification data for quantifiable decisions.
    """

    def __init__(self, db_manager):
        self.db = db_manager

    def get_daily_metrics(self, trading_day: str) -> Dict[str, Any]:
        """
        Retrieves metrics for a specific trading day.
        Format: YYYY-MM-DD
        """
        trades = self.db.get_trade_history(start_date=trading_day, end_date=f"{trading_day} 23:59:59")
        if not trades:
            return {}

        return self._calculate_metrics(trades)

    def get_strategy_performance(self, strategy_name: str, lookback_trades: int = 100) -> Dict[str, Any]:
        """
        Analyzes performance for a specific strategy across recent trades.
        """
        # This assumes strategy_name is stored in the database or can be filtered
        # For now, we fetch all and filter in Python (can be optimized with SQL later)
        all_trades = self.db.get_trades(limit=lookback_trades * 2) # Get enough to cover entry/exits
        
        # Filter trades by strategy (using exit_reason or comment if strategy name is there)
        # Note: In Wave B, we should ensure strategy name is explicitly saved in DB
        strategy_trades = [t for t in all_trades if strategy_name in str(t.get("exit_reason", ""))]
        
        if not strategy_trades:
            return {}

        return self._calculate_metrics(strategy_trades)

    def _calculate_metrics(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Core math for win rate, PnL, and R-multiples."""
        # Focus on EXIT trades for PnL calculation
        exit_trades = [t for t in trades if t.get("type") in ("EXIT", "PARTIAL_EXIT")]
        
        if not exit_trades:
            return {"count": 0}

        df = pd.DataFrame(exit_trades)
        
        pnl_cash = df["pnl_cash"].sum()
        pnl_pts = df["pnl_points"].sum()
        win_rate = (df["pnl_cash"] > 0).mean() * 100
        
        avg_pnl = df["pnl_cash"].mean()
        max_dd = self._calculate_max_drawdown(df["pnl_cash"])
        
        # Calculate Avg R (Reward-to-Risk)
        # This requires stop_loss info which we should ensure is in the DB
        # For now, return the basic L4 requirements
        return {
            "count": len(exit_trades),
            "win_rate": f"{win_rate:.1f}%",
            "net_pnl_cash": f"{pnl_cash:+,.0f}",
            "net_pnl_pts": f"{pnl_pts:+.1f}",
            "avg_pnl": f"{avg_pnl:+.1f}",
            "max_drawdown": f"{max_dd:,.0f}",
        }

    def _calculate_max_drawdown(self, pnl_series: pd.Series) -> float:
        cumulative = pnl_series.cumsum()
        running_max = cumulative.cummax()
        drawdown = running_max - cumulative
        return drawdown.max()
