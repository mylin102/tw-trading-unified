#!/usr/bin/env python3
"""
Clear Simulation Data Utility
Truncates all paper trading ledger files to reset the simulation state.
Keeps the header line to preserve CSV schema.
"""
import os
import glob
import argparse
from pathlib import Path

def truncate_file(file_path):
    """Keep the first line (header) and remove the rest."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            header = f.readline()
        
        if not header:
            print(f"  [!] {file_path} is empty, skipping.")
            return

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(header)
        print(f"  [✓] Cleared: {file_path}")
    except Exception as e:
        print(f"  [✗] Failed to clear {file_path}: {e}")

def main():
    parser = argparse.ArgumentParser(description="Clear paper trading ledgers.")
    parser.add_argument("--force", action="store_true", help="Force clearing without confirmation.")
    args = parser.parse_args()

    if not args.force:
        confirm = input("This will PERMANENTLY clear all simulation trade history. Are you sure? (y/N): ")
        if confirm.lower() != 'y':
            print("Operation cancelled.")
            return

    # ── Target Patterns ──
    targets = [
        "logs/trades/TRADES_FUTURES_*.csv",
        "strategies/options/logs/ledger/OPTIONS_LEDGER_*.csv",
        "strategies/stocks/logs/ledger/STOCK_LEDGER_*.csv",
        "logs/market_data/*_signals_audit.csv"
    ]

    project_root = Path(__file__).parent.parent.parent
    print(f"Cleaning simulation data in {project_root}...")

    found_any = False
    for pattern in targets:
        files = glob.glob(str(project_root / pattern))
        for f in files:
            found_any = True
            truncate_file(f)

    if not found_any:
        print("No simulation data files found.")
    else:
        print("Done.")

if __name__ == "__main__":
    main()
