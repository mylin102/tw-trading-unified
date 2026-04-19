# External Stock Alpha Integration

## Overview
This module integrates external high-alpha stock leaders from the `tw-canslim-web` project to drive the trading system's watchlist.

## Data Source
- **URL**: `https://raw.githubusercontent.com/mylin102/tw-canslim-web/master/data/leaders.json`
- **Format**: JSON with a `universe` key containing a list of stock objects with `symbol`.

## Implementation
- **Sync Script**: `scripts/sync/sync_external_watchlist.py` - Fetches the latest leaders and updates `config/stocks.yaml`.
- **Dry Run Script**: `scripts/runners/dry_run_external_stocks.py` - Tests the integration without permanent changes.

## Dry Run Results (2026-04-19)
- **Status**: ✅ SUCCESS
- **Fetched**: 93 tickers
- **Scan Highlights**:
    - Identified patterns like `CUP_WITH_HANDLE` for multiple tickers (e.g., 1815, 2313, 3037).
    - Pivot prices correctly calculated based on the external list.

## Automation
This update is **automated** via the `autostart.sh` monitoring system. It triggers every day during the maintenance windows:
1.  **Day Session Close**: ~13:45 TJS
2.  **Night Session Close**: ~05:00 TJS

## Manual Usage
To manually update the system with the latest external leaders, run:
```bash
python3 scripts/sync/sync_external_watchlist.py
```
This will automatically trigger a system restart if the monitor is active.
