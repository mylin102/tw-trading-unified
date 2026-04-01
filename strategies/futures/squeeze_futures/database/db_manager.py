"""
Database Manager for SQLite persistence.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


class DatabaseManager:
    """SQLite database manager for trade persistence."""

    _RESET_ONCE_PATHS: set[str] = set()

    _TRADE_COLUMNS = {
        "ticker": "TEXT",
        "direction": "TEXT",
        "type": "TEXT",
        "timestamp": "TEXT",
        "entry_time": "TEXT",
        "exit_time": "TEXT",
        "price": "REAL",
        "entry_price": "REAL",
        "exit_price": "REAL",
        "lots": "INTEGER",
        "pnl_points": "REAL",
        "gross_pnl_cash": "REAL",
        "broker_fee": "REAL",
        "exchange_fee": "REAL",
        "tax_cost": "REAL",
        "total_cost": "REAL",
        "pnl_cash": "REAL",
        "entry_score": "REAL",
        "exit_reason": "TEXT",
    }

    _SNAPSHOT_COLUMNS = {
        "timestamp": "TEXT",
        "balance": "REAL",
        "position": "INTEGER",
        "unrealized_pnl": "REAL",
        "total_equity": "REAL",
        "market_price": "REAL",
    }

    def __init__(self, db_path: str = "data/trading.db"):
        self.db_path = str(db_path)
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        if db_file.name.startswith("test_") and self.db_path not in self._RESET_ONCE_PATHS:
            if db_file.exists():
                db_file.unlink()
            self._RESET_ONCE_PATHS.add(self.db_path)
        self._init_schema()

    def _get_connection(self):
        """Get SQLite connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        """Initialize database schema and backfill missing columns."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT,
                    direction TEXT,
                    type TEXT,
                    timestamp TEXT,
                    entry_time TEXT,
                    exit_time TEXT,
                    price REAL,
                    entry_price REAL,
                    exit_price REAL,
                    lots INTEGER,
                    pnl_points REAL,
                    gross_pnl_cash REAL,
                    broker_fee REAL,
                    exchange_fee REAL,
                    tax_cost REAL,
                    total_cost REAL,
                    pnl_cash REAL,
                    entry_score REAL,
                    exit_reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    balance REAL,
                    position INTEGER,
                    unrealized_pnl REAL,
                    total_equity REAL,
                    market_price REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT,
                    module TEXT,
                    message TEXT,
                    details TEXT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_columns(conn, "trades", self._TRADE_COLUMNS)
            self._ensure_columns(conn, "equity_snapshots", self._SNAPSHOT_COLUMNS)
            conn.commit()

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, column_type in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")

    def record_trade(self, trade: Dict[str, Any]):
        """Record a trade to the database."""
        payload = {
            "ticker": trade.get("ticker", ""),
            "direction": trade.get("direction", ""),
            "type": trade.get("type", ""),
            "timestamp": self._serialize_value(
                trade.get("timestamp") or trade.get("exit_time") or trade.get("entry_time") or ""
            ),
            "entry_time": self._serialize_value(trade.get("entry_time")),
            "exit_time": self._serialize_value(trade.get("exit_time")),
            "price": trade.get("price", trade.get("exit_price", trade.get("entry_price", 0))),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "lots": trade.get("lots", 0),
            "pnl_points": trade.get("pnl_points"),
            "gross_pnl_cash": trade.get("gross_pnl_cash"),
            "broker_fee": trade.get("broker_fee"),
            "exchange_fee": trade.get("exchange_fee"),
            "tax_cost": trade.get("tax_cost"),
            "total_cost": trade.get("total_cost"),
            "pnl_cash": trade.get("pnl_cash", 0),
            "entry_score": trade.get("entry_score"),
            "exit_reason": trade.get("exit_reason"),
        }
        columns = list(payload.keys())
        placeholders = ", ".join("?" for _ in columns)
        with self._get_connection() as conn:
            conn.execute(
                f"INSERT INTO trades ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(payload[column] for column in columns),
            )
            conn.commit()

    def _serialize_value(self, value: Any) -> Any:
        if hasattr(value, "isoformat"):
            return value.isoformat(sep=" ")
        return value

    def record_equity_snapshot(
        self,
        timestamp: str,
        balance: float,
        position: int,
        unrealized_pnl: float = 0,
        total_equity: Optional[float] = None,
        market_price: Optional[float] = None,
    ):
        """Record an equity snapshot."""
        total_equity = balance + unrealized_pnl if total_equity is None else total_equity
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO equity_snapshots (
                    timestamp, balance, position, unrealized_pnl, total_equity, market_price
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self._serialize_value(timestamp),
                    balance,
                    position,
                    unrealized_pnl,
                    total_equity,
                    market_price,
                ),
            )
            conn.commit()

    def save_equity_snapshot(self, **kwargs):
        """Backward-compatible alias used by the simulator."""
        self.record_equity_snapshot(**kwargs)

    def log_system_event(self, level: str, module: str, message: str, details: Optional[str] = None):
        """Log a system event."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO system_logs (level, module, message, details)
                VALUES (?, ?, ?, ?)
                """,
                (level, module, message, details),
            )
            conn.commit()

    def get_trades(self, ticker: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Get trades from the database."""
        query = "SELECT * FROM trades"
        params: list[Any] = []
        if ticker:
            query += " WHERE ticker = ?"
            params.append(ticker)
        query += " ORDER BY COALESCE(exit_time, entry_time, timestamp) DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._get_connection() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_trade_history(
        self,
        ticker: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Backward-compatible trade history API."""
        query = "SELECT * FROM trades WHERE 1=1"
        params: list[Any] = []
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        if start_date:
            query += " AND COALESCE(exit_time, entry_time, timestamp) >= ?"
            params.append(start_date)
        if end_date:
            query += " AND COALESCE(exit_time, entry_time, timestamp) <= ?"
            params.append(end_date)
        query += " ORDER BY COALESCE(exit_time, entry_time, timestamp), id LIMIT ?"
        params.append(limit)
        with self._get_connection() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_performance_summary(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate completed-trade performance."""
        query = """
            SELECT
                COUNT(*) AS total_trades,
                COALESCE(SUM(pnl_cash), 0) AS net_profit,
                COALESCE(SUM(CASE WHEN pnl_cash > 0 THEN 1 ELSE 0 END), 0) AS winning_trades,
                COALESCE(SUM(CASE WHEN pnl_cash < 0 THEN 1 ELSE 0 END), 0) AS losing_trades,
                COALESCE(SUM(CASE WHEN pnl_cash > 0 THEN pnl_cash ELSE 0 END), 0) AS gross_profit,
                COALESCE(ABS(SUM(CASE WHEN pnl_cash < 0 THEN pnl_cash ELSE 0 END)), 0) AS gross_loss
            FROM trades
            WHERE type IN ('EXIT', 'PARTIAL_EXIT', 'PARTIAL')
        """
        params: list[Any] = []
        if start_date:
            query += " AND COALESCE(exit_time, entry_time, timestamp) >= ?"
            params.append(start_date)
        if end_date:
            query += " AND COALESCE(exit_time, entry_time, timestamp) <= ?"
            params.append(end_date)
        with self._get_connection() as conn:
            row = dict(conn.execute(query, params).fetchone())
        total_trades = row["total_trades"] or 0
        gross_loss = row["gross_loss"] or 0
        return {
            "total_trades": total_trades,
            "net_profit": row["net_profit"] or 0,
            "winning_trades": row["winning_trades"] or 0,
            "losing_trades": row["losing_trades"] or 0,
            "win_rate": ((row["winning_trades"] or 0) / total_trades * 100) if total_trades else 0,
            "gross_profit": row["gross_profit"] or 0,
            "gross_loss": gross_loss,
            "profit_factor": ((row["gross_profit"] or 0) / gross_loss) if gross_loss else float("inf"),
        }

    def get_equity_curve(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """Get equity curve data."""
        with self._get_connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM equity_snapshots ORDER BY timestamp DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
