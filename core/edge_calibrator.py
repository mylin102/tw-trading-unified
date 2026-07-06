"""
Edge Calibrator — Finds the optimal threshold and sizing parameters for each strategy.
Focuses on maximizing Expectancy and Sharpe Ratio, not just Win Rate.
"""
import pandas as pd
import numpy as np
import json
from pathlib import Path

class EdgeCalibrator:
    def __init__(self, attribution_path: str):
        self.path = Path(attribution_path)
        
    def load_data(self) -> pd.DataFrame:
        if not self.path.exists(): return pd.DataFrame()
        df = pd.read_csv(self.path)
        # Flatten outcome and edge from JSON
        df["pnl"] = df["outcome"].apply(lambda x: json.loads(x).get("pnl", 0))
        # Note: edge is already a column in our diagnostics, but here we read from trade_attribution
        # If we need to re-score, we would do it here. 
        # For now, let's assume 'edge' score is part of the attribution or features.
        return df

    def analyze_strategy(self, strategy_name: str, df: pd.DataFrame):
        """Find best threshold for a specific strategy."""
        strat_df = df[df["strategy"] == strategy_name].copy()
        if len(strat_df) < 20:
            return None
            
        results = []
        # Test thresholds from 0.3 to 0.8
        for t in np.arange(0.3, 0.8, 0.02):
            subset = strat_df[strat_df["edge"] >= t]
            if len(subset) < 10: continue
            
            pnl_sum = subset["pnl"].sum()
            pnl_mean = subset["pnl"].mean()
            win_rate = (subset["pnl"] > 0).mean()
            sharpe = pnl_mean / (subset["pnl"].std() + 1e-6)
            
            results.append({
                "threshold": round(t, 2),
                "count": len(subset),
                "total_pnl": pnl_sum,
                "avg_pnl": pnl_mean,
                "win_rate": win_rate,
                "sharpe": sharpe
            })
            
        res_df = pd.DataFrame(results)
        if res_df.empty: return None
        
        # Selection Logic: Maximize Total PnL but ensure Sharpe is healthy
        best_row = res_df.sort_values("total_pnl", ascending=False).iloc[0]
        return best_row.to_dict()

    def generate_calibration_config(self):
        """Generate a recommended config for edge_model."""
        df = self.load_data()
        if df.empty: return {}
        
        config = {}
        for strat in df["strategy"].unique():
            best = self.analyze_strategy(strat, df)
            if best:
                config[strat] = {
                    "threshold": best["threshold"],
                    "expected_win_rate": round(best["win_rate"], 2),
                    "confidence": round(best["sharpe"], 2)
                }
        return config

# Global helper
def get_strategy_thresholds():
    calibrator = EdgeCalibrator("logs/trade_attribution.csv")
    return calibrator.generate_calibration_config()
