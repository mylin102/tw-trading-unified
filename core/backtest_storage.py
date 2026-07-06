"""
Experiment Tracking System — Structured storage for backtest results.
Persists trades, equity curves, and metrics with versioning and git tracking.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from core.backtest_engine import BacktestResult


class ExperimentTracker:
    """Manages the storage and retrieval of backtest experiments."""

    def __init__(self, base_path: str = "data/backtests", logger: Optional[logging.Logger] = None):
        self.base_path = Path(base_path)
        self.exp_path = self.base_path / "experiments"
        self.registry_file = self.base_path / "registry.json"
        self.logger = logger or logging.getLogger(__name__)

        # Ensure directories exist
        self.exp_path.mkdir(parents=True, exist_ok=True)
        if not self.registry_file.exists():
            self._save_registry([])

    def _get_git_hash(self) -> str:
        try:
            return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode("ascii").strip()
        except:
            return "unknown"

    def _load_registry(self) -> List[Dict[str, Any]]:
        try:
            with open(self.registry_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to load registry: {e}")
            return []

    def _save_registry(self, registry: List[Dict[str, Any]]):
        with open(self.registry_file, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)

    def save_experiment(
        self,
        result: BacktestResult,
        params: Dict[str, Any],
        tag: Optional[str] = None
    ) -> str:
        """
        Save a backtest result as a versioned experiment.
        Returns the generated experiment ID.
        """
        now = datetime.now()
        timestamp_id = now.strftime("%Y%m%d_%H%M%S")
        strategy_clean = result.strategy_name.replace(" ", "_").lower()
        exp_id = f"EXP_{timestamp_id}_{strategy_clean}"
        if tag:
            exp_id += f"_{tag}"

        save_dir = self.exp_path / exp_id
        save_dir.mkdir(parents=True, exist_ok=True)

        # 1. Save Trades (Parquet)
        if not result.trades.empty:
            result.trades.to_parquet(save_dir / "trades.parquet", index=False)

        # 2. Save Equity Curve (Parquet)
        if not result.equity_curve.empty:
            result.equity_curve.to_frame("equity").to_parquet(save_dir / "equity.parquet")

        # 3. Save Meta (JSON)
        meta = {
            "exp_id": exp_id,
            "timestamp": now.isoformat(),
            "strategy": result.strategy_name,
            "metrics": result.metrics,
            "params": params,
            "git_hash": self._get_git_hash(),
            "tag": tag
        }
        with open(save_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        # 4. Update Registry
        registry = self._load_registry()
        registry.insert(0, meta)  # Newest first
        self._save_registry(registry)

        self.logger.info(f"Experiment saved: {exp_id}")
        return exp_id

    def list_experiments(self, strategy: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all experiments, optionally filtered by strategy."""
        registry = self._load_registry()
        if strategy:
            return [e for e in registry if e["strategy"] == strategy]
        return registry

    def load_result(self, exp_id: str) -> Optional[tuple[BacktestResult, Dict[str, Any]]]:
        """Load an experiment result and its metadata from disk."""
        load_dir = self.exp_path / exp_id
        if not load_dir.exists():
            self.logger.error(f"Experiment {exp_id} not found.")
            return None

        try:
            # 1. Load Meta
            with open(load_dir / "meta.json", "r", encoding="utf-8") as f:
                meta = json.load(f)

            # 2. Load Trades
            trades_file = load_dir / "trades.parquet"
            trades = pd.read_parquet(trades_file) if trades_file.exists() else pd.DataFrame()

            # 3. Load Equity
            equity_file = load_dir / "equity.parquet"
            equity = pd.read_parquet(equity_file)["equity"] if equity_file.exists() else pd.Series()

            result = BacktestResult(
                strategy_name=meta["strategy"],
                trades=trades,
                equity_curve=equity,
                metrics=meta["metrics"]
            )
            return result, meta
        except Exception as e:
            self.logger.error(f"Failed to load experiment {exp_id}: {e}")
            return None

    def delete_experiment(self, exp_id: str):
        """Delete an experiment from disk and registry."""
        shutil.rmtree(self.exp_path / exp_id, ignore_errors=True)
        registry = self._load_registry()
        registry = [e for e in registry if e["exp_id"] != exp_id]
        self._save_registry(registry)
        self.logger.info(f"Experiment deleted: {exp_id}")


# Singleton instance for easy import
tracker = ExperimentTracker()
