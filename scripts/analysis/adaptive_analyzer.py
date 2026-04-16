#!/usr/bin/env python3
"""
Adaptive Analyzer — correlates trades, signals, and indicators to find alpha.
Follows GSD Spec for Wave 5.1.
"""
import pandas as pd
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

class AdaptiveAnalyzer:
    def __init__(self, date_str: str, ticker: str = "TMF"):
        self.date_str = date_str
        self.ticker = ticker
        self.base_dir = Path(__file__).resolve().parent.parent.parent
        
        # Input Paths
        self.trades_file = self.base_dir / "exports" / "trades" / f"{ticker}_{date_str}_trades.csv"
        self.audit_file = self.base_dir / "logs" / "market_data" / f"{ticker}_{date_str}_signals_audit.csv"
        self.indicator_file = self.base_dir / "logs" / "market_data" / f"OPTIONS_{date_str}_indicators.csv"
        
        # Output Paths
        self.output_dir = self.base_dir / "logs" / "analysis"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.report_file = self.output_dir / f"adaptive_review_{date_str}.json"

    def load_data(self) -> Dict[str, pd.DataFrame]:
        data = {}
        if self.trades_file.exists():
            data['trades'] = pd.read_csv(self.trades_file, parse_dates=['timestamp'])
        if self.audit_file.exists():
            data['audit'] = pd.read_csv(self.audit_file, parse_dates=['timestamp'])
        if self.indicator_file.exists():
            data['indicators'] = pd.read_csv(self.indicator_file, parse_dates=['timestamp'])
        return data

    def correlate_trades(self, data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """Join trades with nearest indicators to find features at entry."""
        if 'trades' not in data or 'indicators' not in data:
            return pd.DataFrame()
            
        trades = data['trades'].copy()
        indicators = data['indicators'].copy()
        
        # Sort for merge_asof
        trades = trades.sort_values('timestamp')
        indicators = indicators.sort_values('timestamp')
        
        # Correlate ENTRY trades with indicators
        enriched = pd.merge_asof(
            trades, 
            indicators, 
            on='timestamp', 
            direction='backward'
        )
        return enriched

    def analyze_reason_alpha(self, enriched_df: pd.DataFrame) -> Dict:
        """Quantify win rate and avg PnL per entry reason."""
        if enriched_df.empty:
            return {}
            
        # We need to link ENTRY and EXIT to get PnL per reason
        # Simplified: Use existing pnl_cash in trade records if available
        summary = {}
        reasons = enriched_df['reason'].unique()
        
        for reason in reasons:
            if not reason: continue
            subset = enriched_df[enriched_df['reason'] == reason]
            # Focus on EXITS to see realized PnL
            exits = subset[subset['type'].str.contains('EXIT', na=False)]
            
            summary[str(reason)] = {
                "count": int(len(subset)),
                "exits": int(len(exits)),
                "total_pnl": float(exits['pnl_cash'].sum()) if 'pnl_cash' in exits else 0.0,
                "win_rate": float(len(exits[exits['pnl_cash'] > 0]) / len(exits)) if not exits.empty else 0.0
            }
        return summary

    def run(self):
        print(f"Starting Adaptive Analysis for {self.date_str}...")
        data = self.load_data()
        
        if not data:
            print("❌ No data files found.")
            return
            
        enriched = self.correlate_trades(data)
        alpha_report = self.analyze_reason_alpha(enriched)
        
        report = {
            "metadata": {
                "date": self.date_str,
                "ticker": self.ticker,
                "analyzed_at": datetime.now().isoformat()
            },
            "reason_alpha": alpha_report,
            "observations": self.generate_observations(enriched, data.get('audit'))
        }
        
        with open(self.report_file, 'w') as f:
            json.dump(report, f, indent=2)
            
        print(f"✅ Report generated: {self.report_file}")
        return report

    def generate_observations(self, enriched: pd.DataFrame, audit: Optional[pd.DataFrame]) -> List[str]:
        obs = []
        if audit is not None:
            # Audit CSV uses 'signal' column, not 'type'
            signal_col = 'signal' if 'signal' in audit.columns else 'type'
            blocked = audit[audit[signal_col] == 'ENTRY_BLOCKED']
            obs.append(f"Blocked {len(blocked)} signals today.")
            
            # Audit CSV uses 'rejection' for the detail, or 'reason'
            reason_col = 'rejection' if 'rejection' in audit.columns else 'reason'
            cooldowns = blocked[blocked[reason_col].str.contains('cooldown', na=False)]
            if len(cooldowns) > 10:
                obs.append("High frequency of cooldowns detected. Market may be choppy.")
                
        return obs

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    args = parser.parse_args()
    
    analyzer = AdaptiveAnalyzer(args.date)
    analyzer.run()
