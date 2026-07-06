
import pandas as pd
import numpy as np

def analyze_relaxed_strictness(file_path):
    df = pd.read_csv(file_path)
    total_bars = len(df)
    
    print(f"📊 Relaxed Analysis ({total_bars} bars)")
    print("-" * 50)
    
    # Relaxed Thresholds
    MAX_MOM = 2
    MAX_MACD = 50.0  # Increased from 0.5
    VOL_THRESHOLD = 1.5 # Fixed bug (was truthiness)
    
    theta_mom_blocked = df[df['mom_state'] > MAX_MOM]
    theta_macd_blocked = df[np.abs(df['macd_hist']) >= MAX_MACD]
    theta_vol_blocked = df[df['volume_spike'] > VOL_THRESHOLD]
    
    theta_any_blocked = df[(df['mom_state'] > MAX_MOM) | (np.abs(df['macd_hist']) >= MAX_MACD) | (df['volume_spike'] > VOL_THRESHOLD)]
    
    print(f"RELAXED Option Selling (Theta) Strictness:")
    print(f"  - Mom State > {MAX_MOM}: {len(theta_mom_blocked)} bars blocked ({len(theta_mom_blocked)/total_bars*100:.1f}%)")
    print(f"  - MACD Hist >= {MAX_MACD}: {len(theta_macd_blocked)} bars blocked ({len(theta_macd_blocked)/total_bars*100:.1f}%)")
    print(f"  - Volume Spike > {VOL_THRESHOLD}: {len(theta_vol_blocked)} bars blocked ({len(theta_vol_blocked)/total_bars*100:.1f}%)")
    print(f"  => TOTAL THETA BLOCKED: {len(theta_any_blocked)} / {total_bars} bars ({len(theta_any_blocked)/total_bars*100:.1f}%)")
    print(f"  => POTENTIAL ENTRIES: {total_bars - len(theta_any_blocked)}")

if __name__ == "__main__":
    file_path = "logs/market_data/MXF_20260507_PAPER_indicators.csv"
    analyze_relaxed_strictness(file_path)
