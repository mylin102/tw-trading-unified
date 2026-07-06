"""
Daily Archiver — Wave 18.3.
Consolidates daily CSV logs into the permanent Parquet database (SSOT).
Supports TMF (Futures) and OPTIONS data pipelines.
"""
import os
import pandas as pd
from pathlib import Path
import sys

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.data_manager import data_manager

def archive_all():
    log_dir = ROOT / "logs" / "market_data"
    if not log_dir.exists():
        print(f"⚠️ Log directory not found: {log_dir}")
        return

    # Define Archiving Targets
    targets = [
        {"pattern": "TMF_*_indicators.csv", "ticker": "TXFR1"},
        {"pattern": "OPTIONS_*_indicators.csv", "ticker": "OPTIONS"}
    ]

    backup_dir = ROOT / "data" / "archive"
    backup_dir.mkdir(parents=True, exist_ok=True)

    for target in targets:
        csv_files = list(log_dir.glob(target["pattern"]))
        if not csv_files:
            print(f" ℹ️ No new data for {target['ticker']}.")
            continue
        
        print(f"📦 Consolidating {target['ticker']} ({len(csv_files)} files)...")
        all_dfs = []
        for f in csv_files:
            try:
                # Read CSV and ensure timestamp is the index
                df = pd.read_csv(f, parse_dates=["timestamp"])
                if not df.empty:
                    df = df.set_index("timestamp")
                    all_dfs.append(df)
            except Exception as e:
                print(f" ❌ Error reading {f.name}: {e}")
        
        if all_dfs:
            combined_new = pd.concat(all_dfs).sort_index()
            # Deduplicate (Keep latest record for each timestamp)
            combined_new = combined_new[~combined_new.index.duplicated(keep='last')]
            
            # Merge into permanent Parquet DB via DataManager (Atomic Write)
            # This handles loading the existing Parquet and merging the new data
            try:
                # For now, we'll manually merge here or ensure data_manager.save_historical 
                # handles merging. Looking at core/data_manager.py, we need to ensure 
                # it doesn't just overwrite.
                
                # Check existing
                existing_df = data_manager.load_historical(target["ticker"])
                if not existing_df.empty:
                    final_df = pd.concat([existing_df, combined_new]).sort_index()
                    final_df = final_df[~final_df.index.duplicated(keep='last')]
                else:
                    final_df = combined_new
                
                data_manager.save_historical(target["ticker"], final_df)
                
                # Move to archive folder
                for f in csv_files:
                    os.replace(f, backup_dir / f.name)
                print(f" ✅ {target['ticker']} SSOT updated. {len(combined_new)} bars archived.")
            except Exception as e:
                print(f" ❌ Failed to update Parquet for {target['ticker']}: {e}")

if __name__ == "__main__":
    archive_all()
