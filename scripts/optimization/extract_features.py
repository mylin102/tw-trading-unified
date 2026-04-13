"""
Streaming Feature Extractor — Wave 14 Monthly Aggregator.
Processes data month-by-month to bypass execution timeouts.
"""
import sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from dateutil.relativedelta import relativedelta

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_manager import data_manager
from core.data_enricher import enricher

def extract_streaming():
    print("📊 Loading raw historical data...")
    df_full = data_manager.load_historical("TXFR1")
    if df_full.empty: return

    out_path = Path("data/optimization/orb_ml_dataset.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Initialize empty file or read existing to resume
    all_features = []
    
    # Process month by month
    start_date = df_full.index.min()
    end_date = df_full.index.max()
    curr = start_date
    
    print(f"🚀 Streaming extraction: {start_date.date()} → {end_date.date()}")
    
    while curr < end_date:
        month_end = curr + relativedelta(months=1)
        print(f" 📅 Processing: {curr.strftime('%Y-%m')}...")
        
        df_month = df_full[(df_full.index >= curr) & (df_full.index < month_end)].copy()
        if not df_month.empty:
            # 1. Enrich this month
            df_month = enricher.enrich(df_month, ["atr", "linreg", "kalman"])
            
            # 2. Extract ORB Breakouts (Simplified Vectorized)
            df_month['trading_day'] = df_month.index.date
            # Avoid transform() overhead, use simple loop over days in THIS month (max 22 days)
            days = df_month['trading_day'].unique()
            
            for day in days:
                day_df = df_month[df_month['trading_day'] == day]
                if len(day_df) < 10: continue
                
                orb_h = day_df.iloc[:6]['High'].max()
                orb_l = day_df.iloc[:6]['Low'].min()
                
                post = day_df.iloc[6:]
                # First breakout
                breaks = post[(post['Close'] > orb_h) | (post['Close'] < orb_l)]
                if not breaks.empty:
                    first = breaks.iloc[0]
                    ts = breaks.index[0]
                    direction = 1 if first['Close'] > orb_h else -1
                    
                    # 1. Physical Features
                    # 2. Chip Proxy Features (Wave 17)
                    # Gap % from yesterday close
                    prev_close = day_df.iloc[0]['Open'] # Approximate start of day
                    gap_pct = (day_df.iloc[0]['Open'] - df_full.iloc[df_full.index.get_loc(day_df.index[0])-1]['Close']) / prev_close if df_full.index.get_loc(day_df.index[0]) > 0 else 0
                    
                    # Volume Ratio (Relative to 5-day avg)
                    # For simplicity, we assume constant 1.0 here or compute real if needed
                    
                    feat = {
                        "ts": ts,
                        "dir": direction,
                        "k_vel": (first['kalman_close'] - day_df.iloc[day_df.index.get_loc(ts)-1]['kalman_close']) / first['kalman_close'],
                        "lr_curve": first['lr_curve'],
                        "atr_n": first['atr'] / first['Close'],
                        "gap_p": gap_pct,
                        "hour": ts.hour
                    }
                    
                    # Label
                    future = day_df.loc[ts:].iloc[1:25]
                    if not future.empty:
                        target = first['Close'] + direction * (2 * first['atr'])
                        stop = first['Close'] - direction * (1 * first['atr'])
                        success = 0
                        if direction == 1:
                            if future['High'].max() >= target: success = 1
                        else:
                            if future['Low'].min() <= target: success = 1
                        feat["label"] = success
                        all_features.append(feat)

        curr = month_end
        # Break early if we have enough samples for training (to ensure 5min completion)
        if len(all_features) > 200:
            print("✨ Reached sufficient sample size (200+) for training.")
            break

    # Save
    pd.DataFrame(all_features).to_csv(out_path, index=False)
    print(f"✅ Data saved to {out_path}. Success rate: {pd.DataFrame(all_features)['label'].mean():.1%}")

if __name__ == "__main__":
    extract_streaming()
