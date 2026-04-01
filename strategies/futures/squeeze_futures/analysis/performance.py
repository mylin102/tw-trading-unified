"""
Performance Analyzer for trading results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from squeeze_futures.database.db_manager import DatabaseManager


class PerformanceAnalyzer:
    """Analyze trading performance metrics."""

    def __init__(self, trades_or_db_path: List[Dict[str, Any]] | str, initial_balance: float = 100000):
        self.initial_balance = initial_balance
        self.trades: List[Dict[str, Any]] = []
        self.db: DatabaseManager | None = None

        if isinstance(trades_or_db_path, str):
            self.db = DatabaseManager(trades_or_db_path)
        else:
            self.trades = list(trades_or_db_path)

    def load_trades(self) -> List[Dict[str, Any]]:
        """Load completed trades from SQLite if configured."""
        if self.db is None:
            return self.trades
        all_trades = self.db.get_trade_history()
        self.trades = [t for t in all_trades if t.get("type") in {"EXIT", "PARTIAL_EXIT", "PARTIAL"}]
        return self.trades

    def calculate_metrics(self) -> Dict[str, Any]:
        """Calculate performance metrics."""
        if not self.trades:
            return self._empty_metrics()

        pnls = [t.get("pnl_cash", 0) or 0 for t in self.trades]
        winning_trades = [p for p in pnls if p > 0]
        losing_trades = [p for p in pnls if p < 0]

        total_pnl = sum(pnls)
        gross_profit = sum(winning_trades) if winning_trades else 0
        gross_loss = abs(sum(losing_trades)) if losing_trades else 0

        return {
            "total_trades": len(self.trades),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": len(winning_trades) / len(self.trades) * 100 if self.trades else 0,
            "total_pnl": total_pnl,
            "net_profit": total_pnl,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
            "average_win": np.mean(winning_trades) if winning_trades else 0,
            "average_loss": np.mean(losing_trades) if losing_trades else 0,
            "final_balance": self.initial_balance + total_pnl,
        }

    def get_trade_statistics(self) -> Dict[str, Any]:
        """Backward-compatible alias used by scripts/tests."""
        if self.db is not None and not self.trades:
            self.load_trades()
        return self.calculate_metrics()

    def generate_report(self, output_path: str) -> str:
        """Generate a simple markdown report and persist it."""
        stats = self.get_trade_statistics()
        report = (
            "# Performance Report\n\n"
            f"- Total trades: {stats['total_trades']}\n"
            f"- Win rate: {stats['win_rate']:.1f}%\n"
            f"- Net profit: {stats['net_profit']:.2f}\n"
            f"- Profit factor: {stats['profit_factor']:.2f}\n"
            f"- Final balance: {stats['final_balance']:.2f}\n"
        )
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
        return report

    def _empty_metrics(self) -> Dict[str, Any]:
        """Return empty metrics."""
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "net_profit": 0,
            "gross_profit": 0,
            "gross_loss": 0,
            "profit_factor": 0,
            "average_win": 0,
            "average_loss": 0,
            "final_balance": self.initial_balance,
        }
