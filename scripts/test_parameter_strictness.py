
import pandas as pd
import numpy as np

def analyze_strictness(file_path):
    df = pd.read_csv(file_path)
    total_bars = len(df)
    
    print(f"📊 Analyzing {total_bars} bars from {file_path}")
    print("-" * 50)
    
    # 1. Theta Gate (Option Selling)
    # Thresholds from FuturesRouterConfig: theta_max_mom_state = 1, theta_max_macd_abs = 0.5
    theta_mom_blocked = df[df['mom_state'] > 1]
    theta_macd_blocked = df[np.abs(df['macd_hist']) >= 0.5]
    theta_vol_blocked = df[df['volume_spike'] == True]
    
    # Combined Theta Block (using rough estimation of logical OR)
    theta_any_blocked = df[(df['mom_state'] > 1) | (np.abs(df['macd_hist']) >= 0.5) | (df['volume_spike'] == True)]
    
    print(f"Option Selling (Theta) Strictness:")
    print(f"  - Mom State > 1: {len(theta_mom_blocked)} bars blocked ({len(theta_mom_blocked)/total_bars*100:.1f}%)")
    print(f"  - MACD Hist >= 0.5: {len(theta_macd_blocked)} bars blocked ({len(theta_macd_blocked)/total_bars*100:.1f}%)")
    print(f"  - Volume Spike: {len(theta_vol_blocked)} bars blocked ({len(theta_vol_blocked)/total_bars*100:.1f}%)")
    print(f"  => TOTAL THETA BLOCKED: {len(theta_any_blocked)} / {total_bars} bars ({len(theta_any_blocked)/total_bars*100:.1f}%)")
    print()
    
    # 2. Mean Reversion (ADX)
    # range_mean_reversion_v1 uses ADX < 30 (typical)
    adx_blocked = df[df['adx'] > 30]
    print(f"Mean Reversion Strictness:")
    print(f"  - ADX > 30 (Strong Trend): {len(adx_blocked)} bars blocked ({len(adx_blocked)/total_bars*100:.1f}%)")
    print()
    
    # 3. Regime Distribution
    regime_counts = df['regime'].value_counts()
    print(f"Regime Distribution:")
    for regime, count in regime_counts.items():
        print(f"  - {regime}: {count} bars ({count/total_bars*100:.1f}%)")

if __name__ == "__main__":
    file_path = "logs/market_data/MXF_20260507_PAPER_indicators.csv"
    analyze_strictness(file_path)
