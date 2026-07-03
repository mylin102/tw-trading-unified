# ADR-013: MTS Ghost Position Race Condition — Root Cause & Fix

## Status

2026-07-01 — Completed

## Context

MTS (Multi-leg Time Spread) 系統在 crash loop 重啟後，出現幽靈部位（Ghost Position）：

- Dashboard 顯示部位未平倉，但實際上已 exit
- 每 ~5.5 分鐘觸發一次 `RECONCILIATION_FAILURE`
- state file (`/tmp/mts_position_state.json`) 卡在 `has_position=true` 無法恢復

## Root Cause

### Layer 1: Race Condition in `_reset()`

當 exit 完成時：

1. `_reset()` 先寫入 state file (`has_position: false`)
2. 但 **memory (`self._has_position`) 尚未更新為 `False`**
3. 併發的 Heartbeat thread 讀到 memory 仍為 `True`
4. Heartbeat 覆寫 state file 回 `has_position: true`
5. 造成死鎖：memory 為 False → state file 為 True

### Layer 2: Restore Loop Death Spiral

PM2 重啟後：

1. MTS restore 從 `mts_trade_fills.jsonl` 找到該 trade（無 EXIT 紀錄）
2. 設 `_has_position = True`
3. Watchdog 比對 `PaperTrader.position == 0` → 判定 GHOST → 強制 `_reset()`
4. 下個 tick restore 又從 fills log 恢復 → 無限循環

## Fixes Applied

### Fix 1: Write Order in `_reset()` (`tmf_spread.py`)

```
Before: write_file() → memory = False
After:  memory = False → write_file()
```

確保 Heartbeat 讀到的永遠是最新 memory。

### Fix 2: Break Restore Loop (`tmf_spread.py`)

`_restore_position_state()` 新增邏輯：

- 如果 state file (JSON) 存在且明確是 `has_position=false` 或 `FLAT/CLOSE`
- 直接 return `False`，**不降級去讀 fills log**
- 狀態檔視為最終事實（SSOT）

### Fix 3: PaperTrader Sync on Restart (`monitor.py`)

Watchdog reconciliation 新增：

- 如果 `_has_position == True` 但 `self.trader.position == 0`（Paper mode)
- 自動補上 `self.trader.position = 1` 並對齊 entry price
- 避免 watchdog 誤判 mismatch

## Files Changed

- `strategies/plugins/futures/active/tmf_spread.py` — Fix 1 + Fix 2
- `strategies/futures/monitor.py` — Fix 3

## Verification

- `RECONCILIATION_FAILURE` count = 0 after fix
- New trade (`mts-auto-162727-447`) operates normally
- State file correctly reflects `has_position=true` during active trade
- Dashboard shows correct position status

## Prevention

Future occurrences are prevented by:
1. Atomic write ordering (memory → file)
2. State file as SSOT (no fallback to fills log when file explicitly says FLAT)
3. PaperTrader auto-sync on restart prevents watchdog false positives
