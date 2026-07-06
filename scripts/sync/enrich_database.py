"""
Enrich Database — Offline pre-calculation of indicators.
Saves indicators to a persistent Parquet file to avoid re-calculating during optimization.
"""
import sys
import pandas as pd
from pathlib import Path

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_manager import data_manager
from core.data_enricher import enricher

def enrich_db():
    print("📊 Loading raw historical data...")
    df = data_manager.load_historical("TXFR1")
    if df.empty: return

    # To avoid timeout, we process in chunks of 1 year
    years = df.index.year.unique()
    all_chunks = []

    for year in years:
        print(f"🚀 Processing Year: {year}...")
        df_year = df[df.index.year == year].copy()
        
        # Calculate NON-KALMAN indicators first (Fast)
        print(f"  • Calculating Fast Indicators (ATR, Linreg)...")
        df_year = enricher.enrich(df_year, ["atr", "linreg", "squeeze"])
        
        # Calculate Kalman (Slow - Python loop)
        # Note: If this still times out, we might need a more optimized C-version
        print(f"  • Calculating Kalman (Wait...)...")
        df_year = enricher.enrich(df_year, ["kalman"])
        
        all_chunks.append(df_year)

    print("💾 Saving Enriched Database...")
    final_df = pd.concat(all_chunks)
    out_path = Path("data/historical/TXFR1_5m_enriched.parquet")
    final_df.to_parquet(out_path, compression='snappy')
    print(f"✅ Enriched data saved to {out_path}")

if __name__ == "__main__":
    enrich_db()
