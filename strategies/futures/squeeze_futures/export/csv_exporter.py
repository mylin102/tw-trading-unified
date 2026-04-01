"""
CSV Exporter for trade data.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from squeeze_futures.database.db_manager import DatabaseManager


class CSVExporter:
    """Export trading data to CSV files."""

    def __init__(self, db_path: Optional[str] = None, output_dir: str = "exports"):
        self.db_path = db_path
        self.db = DatabaseManager(db_path) if db_path else None
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_trades(self, trades: List[Dict[str, Any]], filename: str = "trades.csv"):
        """Export trades to CSV."""
        filepath = self.output_dir / filename
        if not trades:
            filepath.write_text("", encoding="utf-8")
            return filepath

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=trades[0].keys())
            writer.writeheader()
            writer.writerows(trades)

        return filepath

    def export_all_trades(self, filename: str = "all_trades.csv"):
        """Export all recorded trades from SQLite."""
        trades = self.db.get_trade_history() if self.db else []
        return self.export_trades(trades, filename=filename)

    def export_equity_curve(self, equity_data: List[Dict[str, Any]], filename: str = "equity.csv"):
        """Export equity curve to CSV."""
        filepath = self.output_dir / filename
        if not equity_data:
            filepath.write_text("", encoding="utf-8")
            return filepath

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=equity_data[0].keys())
            writer.writeheader()
            writer.writerows(equity_data)

        return filepath
