"""
Strategy Health Monitor — Wave 1.1 (Step 1).
Analyzes trade logs to detect performance degradation.
Enforces 'Rolling 30d PF < 1.0 -> Demote' and '90d PF < 0.8 -> Retire'.
"""
import sys
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
import argparse

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.decision_logger import log_decision

def analyze_health(strategy_name: str, trades_df: pd.DataFrame, dry_run: bool = False):
    print(f"\n🩺 Analyzing Health for: {strategy_name}")
    
    if trades_df.empty:
        print(" ⚠️ No trade data found.")
        return

    # Ensure timestamp is datetime
    trades_df['timestamp'] = pd.to_datetime(trades_df['timestamp'])
    trades_df = trades_df.sort_values('timestamp')
    now = datetime.now()

    # 1. Calculate Rolling Metrics
    # 30-day window
    mask30 = trades_df['timestamp'] > (now - timedelta(days=30))
    df30 = trades_df[mask30]
    
    # 90-day window
    mask90 = trades_df['timestamp'] > (now - timedelta(days=90))
    df90 = trades_df[mask90]

    def calc_pf(df):
        wins = df[df['pnl'] > 0]['pnl'].sum()
        losses = abs(df[df['pnl'] < 0]['pnl'].sum())
        return wins / losses if losses > 0 else (99.0 if wins > 0 else 0.0)

    pf30 = calc_pf(df30)
    pf90 = calc_pf(df90)
    
    print(f" 📊 Rolling 30d PF: {pf30:.2f} ({len(df30)} trades)")
    print(f" 📊 Rolling 90d PF: {pf90:.2f} ({len(df90)} trades)")

    # 2. Threshold Check (SOP v1.1)
    status = "PASS"
    reason = ""

    if pf90 < 0.8 and len(df90) >= 5:
        status = "RETIRE"
        reason = f"90d PF {pf90:.2f} < 0.8 (DEGRADED)"
    elif pf30 < 1.0 and len(df30) >= 3:
        status = "WARN"
        reason = f"30d PF {pf30:.2f} < 1.0 (UNDERPERFORMING)"

    # 3. Action
    print(f" 🏁 Verdict: {status}")
    if status != "PASS":
        print(f" 🚩 Reason: {reason}")
        if not dry_run:
            log_decision(
                action="health_check",
                strategy=strategy_name,
                reason=reason,
                author="system",
                risk_level="medium" if status == "WARN" else "high",
                notes=f"Automatic health verdict: {status}"
            )

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", help="Specific strategy to check")
    parser.add_argument("--dry-run", action="store_true", help="Report only, don't log decisions")
    args = parser.parse_args()

    # Locate trade logs (Standardizing on data/backtests/registry.json or individual CSVs)
    # For this implementation, we check logs/trades/ folder
    log_dir = ROOT / "logs" / "trades"
    if not log_dir.exists():
        # Fallback to check the unified_runner exports
        log_dir = ROOT / "exports" / "backtest"
        
    print(f"🔍 Scanning logs in: {log_dir}")
    
    # Simulate data for testing if directory empty
    if not any(log_dir.glob("*.csv")):
        print(" ⚠️ No CSV logs found. Ensure you have run backtests or live trades.")
        return

    # Process all CSVs in log_dir
    for log_file in log_dir.glob("*.csv"):
        # Assuming filename contains strategy name
        name = log_file.stem
        if args.strategy and args.strategy not in name: continue
        
        df = pd.read_csv(log_file)
        if 'pnl' in df.columns and 'timestamp' in df.columns:
            analyze_health(name, df, dry_run=args.dry_run)

if __name__ == "__main__":
    main()
