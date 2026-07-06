"""
Edge Diagnostics — Analyzes the quality and distribution of Edge Model decisions.
Used to identify if Decision Intelligence is actually predictive.
"""
from __future__ import annotations
import pandas as pd
import numpy as np

class EdgeDiagnostics:
    def __init__(self):
        self.stats = []

    def record_decision(self, strategy_name: str, edge_score: float, is_exploring: bool, features: dict, result_pnl: float | None = None):
        """Record an edge evaluation and its eventual outcome."""
        self.stats.append({
            "strategy": strategy_name,
            "edge": edge_score,
            "explore": is_exploring,
            "features": features,
            "pnl": result_pnl
        })

    def report(self) -> dict:
        """Generate a diagnostic report of the edge model's performance."""
        if not self.stats:
            return {"error": "No data recorded"}
            
        df = pd.DataFrame(self.stats)
        
        # 1. Edge Distribution
        dist = {
            "mean": df["edge"].mean(),
            "std": df["edge"].std(),
            "min": df["edge"].min(),
            "max": df["edge"].max(),
            "q25": df["edge"].quantile(0.25),
            "q75": df["edge"].quantile(0.75)
        }
        
        # 2. Predictive Power (Correlation between Edge and PnL)
        completed_trades = df.dropna(subset=["pnl"])
        correlation = 0.0
        if len(completed_trades) > 5:
            correlation = completed_trades["edge"].corr(completed_trades["pnl"])
            
        # 3. Strategy Calibration Audit
        strategy_stats = df.groupby("strategy")["edge"].agg(["mean", "count"]).to_dict(orient="index")
        
        # 4. Identification of Problems
        problem = "None"
        if dist["std"] < 0.05:
            problem = "LOW_VARIANCE (Edge model is not discriminating)"
        elif correlation < 0:
            problem = "INVERSE_PREDICTION (High edge leads to losses!)"
        elif len(completed_trades) > 10 and correlation < 0.1:
            problem = "NO_PREDICTIVE_POWER (Edge is just noise)"
            
        return {
            "edge_distribution": dist,
            "predictive_correlation": correlation,
            "strategy_audit": strategy_stats,
            "total_evaluations": len(df),
            "completed_trades": len(completed_trades),
            "problem_identified": problem
        }

# Global instance
edge_diag = EdgeDiagnostics()
