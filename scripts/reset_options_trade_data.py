#!/usr/bin/env python3
"""
Reset Options Trade Data
=======================
Clears the options trade ledger and removes old indicator CSVs.
Creates a timestamped backup before resetting.

Usage:
    python3 scripts/reset_options_trade_data.py

The script:
1. Backs up the trade ledger to ./backups/options_reset_<timestamp>/
2. Clears the ledger (writes header only)
3. Removes old indicator CSV files (keeps today's file)
4. Reports what was done

2026-05-25 Hermes Agent: initial implementation.
"""

import shutil
import sys
from datetime import datetime
from pathlib import Path


# ── Paths ──

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = PROJECT_ROOT / "strategies" / "options" / "logs" / "paper_trading"
LEDGER_PATH = LEDGER_DIR / "options_trade_ledger.csv"
BACKUP_ROOT = PROJECT_ROOT / "backups"
INDICATOR_GLOB = "OPTIONS_*_indicators.csv"

LEDGER_HEADER = "trade_id,Timestamp,Mode,Action,Side,Price,Quantity,PnL,Balance,Note"


def confirm_action() -> bool:
    """Ask user for confirmation before destructive operation."""
    print("\n⚠️  WARNING: This will permanently delete all options trade history.")
    print(f"   Ledger:    {LEDGER_PATH}")
    print(f"   Indicator CSVs matching: {INDICATOR_GLOB} in {LEDGER_DIR}")
    print("\nA timestamped backup will be created first.")
    print("Are you sure you want to proceed? [y/N] ", end="", flush=True)
    answer = sys.stdin.readline().strip().lower()
    return answer in ("y", "yes")


def backup_ledger() -> Path | None:
    """Copy current ledger to a timestamped backup directory."""
    if not LEDGER_PATH.exists():
        print("⚠️  No ledger file found — skipping backup.")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUP_ROOT / f"options_reset_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEDGER_PATH, backup_dir / "options_trade_ledger.csv")
    print(f"✅ Backup created: {backup_dir / 'options_trade_ledger.csv'}")
    return backup_dir


def clear_ledger() -> bool:
    """Write header-only ledger to clear all trade records."""
    if not LEDGER_DIR.exists():
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)

    LEDGER_PATH.write_text(LEDGER_HEADER + "\n")
    print(f"✅ Ledger cleared: {LEDGER_PATH} (header only)")
    return True


def remove_old_indicators() -> int:
    """Remove indicator CSV files, keeping today's file."""
    today = datetime.now().strftime("%Y%m%d")
    today_prefix = f"OPTIONS_{today}_"

    removed = 0
    for f in sorted(LEDGER_DIR.glob(INDICATOR_GLOB)):
        if f.name.startswith(today_prefix):
            continue  # Keep today's file — night session will append to it
        f.unlink()
        removed += 1
        print(f"  🗑️  Removed: {f.name}")

    if removed == 0:
        print("  ℹ️  No old indicator files to remove.")
    else:
        print(f"✅ Removed {removed} old indicator CSV(s)")
    return removed


def main():
    print("=" * 60)
    print("  Options Trade Data Reset")
    print("=" * 60)

    # Check that we're in the project root
    if not (PROJECT_ROOT / "strategies" / "options").exists():
        print("❌ Must be run from the project root directory.")
        sys.exit(1)

    # Confirmation
    if not confirm_action():
        print("\n❌ Reset cancelled.")
        sys.exit(0)

    # Execute
    print()
    backup_ledger()
    clear_ledger()
    remove_old_indicators()

    print("\n✅ Options trade data reset complete.")
    print("   Please restart the trading system for changes to take effect:")
    print("   pm2 restart trading-system")
    print()


if __name__ == "__main__":
    main()
