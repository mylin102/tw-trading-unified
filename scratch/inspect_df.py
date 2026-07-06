import sys
import os
import pandas as pd
import yaml
from pathlib import Path

# Setup paths
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from ui.dashboard import load_futures_indicators, load_options_indicators

def inspect():
    print("Loading futures indicators...")
    f_df = load_futures_indicators(full_history=True)
    if f_df is not None:
        print(f"Futures DF shape: {f_df.shape}")
        print("Futures columns:", list(f_df.columns))
        print("Futures timestamp head:")
        print(f_df["timestamp"].head())
        print("Futures close head:")
        close_col = "close" if "close" in f_df.columns else "Close"
        print(f_df[close_col].head())
        print("Futures NaN count in close:", f_df[close_col].isna().sum())
        print("Futures NaN count in timestamp:", f_df["timestamp"].isna().sum())
    else:
        print("Futures DF is None")
        
    print("\nLoading options indicators...")
    o_df = load_options_indicators(full_history=True)
    if o_df is not None:
        print(f"Options DF shape: {o_df.shape}")
        print("Options columns:", list(o_df.columns))
        print("Options timestamp head:")
        print(o_df["timestamp"].head())
        m_col = "price_mtx" if "price_mtx" in o_df.columns else ("mtx_close" if "mtx_close" in o_df.columns else None)
        print(f"Options price column '{m_col}' head:")
        if m_col:
            print(o_df[m_col].head())
            print(f"Options NaN count in {m_col}:", o_df[m_col].isna().sum())
        print("Options NaN count in timestamp:", o_df["timestamp"].isna().sum())
    else:
        print("Options DF is None")

if __name__ == "__main__":
    inspect()
