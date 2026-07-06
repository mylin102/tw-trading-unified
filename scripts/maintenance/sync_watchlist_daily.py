#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
sys.path.append(str(ROOT))

from scripts.sync.sync_external_watchlist import sync

if __name__ == "__main__":
    print("--- [Daily Watchlist Sync] Starting ---")
    sync()
    print("--- [Daily Watchlist Sync] Finished ---")
