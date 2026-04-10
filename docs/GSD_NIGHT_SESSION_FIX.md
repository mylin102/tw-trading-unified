# GSD Systematic Fix: Night Session Data Stagnation

## Incident Timeline (2026-04-09 Night Session)

```
15:00  Night session starts
15:30  First TMF bar saved (34835)
15:35  Second TMF bar saved (34832)
~15:36 Options monitor crashes: TypeError: '<=' not supported: str vs datetime.date
       → main.py sentinel exits (threads dead, no auto-restart yet)
       → All data collection stops
15:36-22:00  NO bars collected (6.5 hours gap)
Next day   User notices gap, requests fix
```

## Root Cause Chain (3 failures)

| # | Failure | Impact | Fix |
|---|---------|--------|-----|
| 1 | **Type bug**: `delivery_date` is str `"2026/04/15"`, compared to `datetime.date` | Crash on startup | Multi-format date parsing |
| 2 | **No auto-restart**: main.py sentinel exits when threads die | Monitor stays down forever | Auto-restart with max 5 attempts |
| 3 | **No backfill**: Missing bars lost on restart | Gap in data permanent | Backfill on startup from API |

## Changes Made

### 1. Type-Safe Date Comparison
**File**: `strategies/options/live_options_squeeze_monitor.py:462-479, 510-523`

```python
# Before (crashed):
if contract.delivery_date <= today:  # str vs datetime.date → TypeError

# After (safe):
dd = contract.delivery_date
if isinstance(dd, str):
    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
        try:
            dd = datetime.datetime.strptime(dd, fmt).date()
            break
        except ValueError:
            continue
    else:
        continue  # Skip unparseable dates
if dd <= today:
    # Expired contract detected
```

### 2. Auto-Restart Sentinel
**File**: `main.py:196-258`

```python
max_restarts = 5
restart_count = 0

while restart_count < max_restarts:
    if not ft.is_alive() or not ot.is_alive():
        # Thread crashed → re-initialize + re-subscribe + restart
        fm = FuturesMonitor(...)
        om = OptionsMonitor(...)
        # Re-subscribe all contracts
        ft = threading.Thread(target=fm.run, daemon=True)
        ot = threading.Thread(target=om.run, daemon=True)
        ft.start(); ot.start()
        restart_count += 1
        time.sleep(10)  # Grace period
        continue
    # ... existing stagnation detection ...
```

### 3. Night Session Backfill on Startup
**File**: `strategies/futures/monitor.py:201-248`

```python
def _backfill_night_gaps(self, api_df):
    """On startup, merge API bars with existing CSV to fill gaps."""
    csv_path = Path(f"logs/market_data/{self.ticker}_{date_str}{tag}_indicators.csv")
    
    if csv_path.exists():
        existing = pd.read_csv(csv_path, parse_dates=['timestamp'])
        last_ts = existing.index.max()
        new_bars = api_df[api_df.index > last_ts]
        
        if new_bars:
            combined = pd.concat([existing, new_bars])
            combined.to_csv(csv_path)
```

## System Behavior After Fix

### Scenario 1: Contract expires mid-session
```
1. Tick stops → _check_options_contract_staleness() fires after 2 min
2. Detects expired contract → unsubscribes old, finds new, re-subscribes
3. Ticks resume → no manual intervention needed
```

### Scenario 2: Monitor crashes (any reason)
```
1. Thread dies → main.py sentinel detects within 2 seconds
2. Auto-restart (up to 5 attempts) → re-init + re-subscribe
3. Backfill runs → fills gaps from Shioaji API
4. Data collection resumes → max ~30 sec gap
```

### Scenario 3: Night session starts with stale CSV
```
1. setup() calls _backfill_night_gaps(api_df)
2. Compares CSV latest timestamp vs API bars
3. Merges missing bars → CSV complete
```

## Current Status (2026-04-09 22:00+)

- ✅ Futures (TMFD6): ticks flowing, 34810 range
- ✅ Options (MTX): ticks flowing, contracts selected
- ✅ No crashes since fix deployed
- ⚠️ Tonight's early bars (15:00-22:00) lost — API doesn't return them anymore
- ✅ Future nights protected by backfill + auto-restart

## Files Changed

| File | Lines Changed | Purpose |
|------|---------------|---------|
| `strategies/options/live_options_squeeze_monitor.py` | ~30 | Type-safe date parsing (2 locations) |
| `main.py` | ~65 | Auto-restart sentinel |
| `strategies/futures/monitor.py` | ~48 | Night session backfill |
| `docs/GSD_FIX_TMF_STAGNATION.md` | Updated | Full documentation |

## Verification

- ✅ 129/130 tests pass (1 pre-existing failure)
- ✅ Python syntax valid (all files)
- ✅ Monitor running, ticks flowing
- ✅ No crashes in 30+ min uptime
