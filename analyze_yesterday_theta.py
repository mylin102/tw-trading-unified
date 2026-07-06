
import pandas as pd
import numpy as np
from pathlib import Path

ledger_path = Path("strategies/options/logs/paper_trading/options_trade_ledger.csv")
indicator_path = Path("strategies/options/logs/paper_trading/OPTIONS_20260416_indicators.csv")

if not ledger_path.exists():
    print(f"Ledger not found at {ledger_path}")
    exit()

df_ledger = pd.read_csv(ledger_path)
df_yesterday = df_ledger[df_ledger["Timestamp"].str.contains("2026-04-15")].copy()

print(f"Yesterday's trades: {len(df_yesterday)}")
theta_exits = df_yesterday[df_yesterday["Action"] == "THETA_EXIT"]
print(f"Theta exits: {len(theta_exits)}")
print(f"Total PnL: {theta_exits['PnL'].sum()}")

# Analyze entries and exits
entries = df_yesterday[df_yesterday["Action"] == "THETA_ENTRY"].copy()
exits = df_yesterday[df_yesterday["Action"] == "THETA_EXIT"].copy()

# Link them (assuming sequential FIFO for simplicity in this analysis)
if len(entries) == len(exits):
    analysis = []
    for i in range(len(entries)):
        en = entries.iloc[i]
        ex = exits.iloc[i]
        analysis.append({
            "entry_time": en["Timestamp"],
            "exit_time": ex["Timestamp"],
            "credit": en["Price"],
            "pnl": ex["PnL"],
            "note": ex["Note"]
        })
    df_ana = pd.DataFrame(analysis)
    print("\nLinked Trade Analysis:")
    print(df_ana)

# Check indicator state during those times
if indicator_path.exists():
    df_ind = pd.read_csv(indicator_path)
    # The user said they were "None", let's check
    null_counts = df_ind.isnull().sum()
    if null_counts.any():
        print("\nNull values in indicators:")
        print(null_counts[null_counts > 0])
    else:
        print("\nNo null values found in indicator file.")
