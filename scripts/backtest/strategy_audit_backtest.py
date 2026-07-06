import os
import sys
import pandas as pd
import numpy as np
import yaml
from pathlib import Path
from datetime import datetime

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.stock_engine import simulate_stock_trades
from backtest.sweep_engine import run_multi_asset_backtest
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze

DATA_DIR = ROOT / "data" / "taifex_raw"

def run_strategy_audit():
    # 超寬鬆參數：強迫進場以測試引擎
    audit_cfg = {
        "stocks": {
            "strategy": "scout_strategy",
            "capital_per_trade": 20000,
            "entry_score": 5, # 極低門檻
            "trailing_stop_pct": 0.015,
            "stop_loss_pct": 0.03,
            "take_profit_pct": 0.05
        }
    }
    stk_cfg = audit_cfg["stocks"]
    
    tickers = [f.stem.split("_")[1] for f in DATA_DIR.glob("STOCK_*_5m.csv")]
    all_dfs = {}
    
    print(f"🔍 Diagnostic: Checking Squeeze activity for {len(tickers)} tickers...")
    
    activity_log = []
    for t in tickers:
        try:
            path = DATA_DIR / f"STOCK_{t}_5m.csv"
            df = pd.read_csv(path)
            date_col = "Date" if "Date" in df.columns else "timestamp"
            df[date_col] = pd.to_datetime(df[date_col]); df = df.set_index(date_col)
            df.columns = [c.capitalize() if c.lower() in ["open", "high", "low", "close", "volume"] else c for c in df.columns]
            
            df = calculate_futures_squeeze(df)
            fires = df["fired"].sum()
            activity_log.append({"Ticker": t, "Fires": fires, "Avg_Volume": df["Volume"].mean()})
            
            if fires > 0:
                all_dfs[t] = df
        except Exception: continue

    activity_df = pd.DataFrame(activity_log)
    print("\n📈 Market Activity Snapshot:")
    print(activity_df.sort_values("Fires", ascending=False).head(10))

    if not all_dfs:
        print("❌ CRITICAL: No Squeeze Fired signals detected in the entire dataset. Strategy entry conditions may be too strict or indicators are miscalculated.")
        return

    summary, ledger = run_multi_asset_backtest(all_dfs, stk_cfg["strategy"], audit_cfg, capital_per_trade=stk_cfg["capital_per_trade"])
    
    if ledger.empty:
        print("❌ No trades even with active fires. Scaling/Position logic might be blocking entries.")
        return

    # 執行原因分析...
    ledger["時間"] = pd.to_datetime(ledger["時間"])
    audit_results = []
    entry_data = {} # {ticker: (time, price)}

    for _, row in ledger.sort_values(["標的", "時間"]).iterrows():
        t = row["標的"]
        if "進場" in row["原因"]:
            entry_data[t] = (row["時間"], row["進場價"])
            continue
        
        if t in entry_data:
            e_time, e_price = entry_data[t]
            duration = (row["時間"] - e_time).total_seconds() / 60
            pnl = row["損益"]
            
            mode = "SUCCESS" if pnl > 0 else "FAILURE"
            if pnl < 0:
                if duration < 30: mode = "Noise Trap"
                elif pnl < -1000: mode = "Deep Loss"
                else: mode = "Other"
            
            audit_results.append({"Ticker": t, "Duration": duration, "PnL": pnl, "Mode": mode})
            del entry_data[t]

    if audit_results:
        audit_df = pd.DataFrame(audit_results)
        print("\n📊 Failure Mode Distribution:")
        print(audit_df["Mode"].value_counts())
    else:
        print("❌ Analysis results are empty.")

if __name__ == "__main__":
    run_strategy_audit()
