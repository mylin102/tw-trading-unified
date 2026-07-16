# Architecture Decision Record: MTS Synchronization and Real-Time Execution Fix

**Date:** 2026-05-26
**Status:** Implemented & Verified
**Context:** Minimal Tradable Spread (MTS) Engine Phase 0

## The Incidents
During paper trading and manual testing of the `tmf_spread` (MTS) strategy, several critical issues emerged:
1. **Ghost Positions:** After an "Emergency Close All" action via the dashboard, the UI table remained populated. Restarting the system caused the position to reappear.
2. **Missing Realized PnL:** The dashboard did not display realized PnL for closed legs (Release phase).
3. **Hardcoded MXF Fallbacks:** The system calculated PnL using the MXF multiplier (50) instead of TMF (10), logging trades incorrectly as "MXF".
4. **Stagnant Trailing Stops (The "Blind Spot"):** The trailing stop failed to trigger despite live prices exceeding the exit threshold. The `far_last` price in the state file was stuck, failing to capture intra-bar (sub-5-minute) flash spikes.

## Root Cause Analysis

### 1. Memory-Disk Desynchronization
The emergency `close_all` logic (via `/tmp/futures_manual_trade.flag`) deleted the disk state but **did not reset the in-memory strategy instance (`_mts_strat`)**. The subsequent heartbeat (every second) immediately overwrote the disk state with the stale in-memory position, bringing the ghost position back to life. Furthermore, the emergency path failed to append an `EXIT` fill to `mts_trade_fills.jsonl`.

### 2. Local Context Isolation (Hardcoded MXF)
The MTS strategy relies on `monitor.py` to construct its `StrategyContext`. At three distinct points (startup, manual trade flag, and post-fill sync), `monitor.py` passed an empty or dummy `MarketData` object that omitted `ticker=self.ticker`. This forced `tmf_spread.py` to fall back to its hardcoded default ("MXF"), polluting the logs and applying a 50x multiplier to TMF trades.

### 3. The 5-Minute Bucket Blind Spot
The most critical flaw was in the event loop (`on_tick`). The MTS management function (`_mts_tick`) was only called when a 5-minute K-bar completed. If a price spiked and retraced within that 5-minute window, the trailing stop logic never saw the peak. Furthermore, the first tick of any new 5-minute bucket was excluded from processing due to a flawed `elif ts_int == self._last_bar_ts:` guard.
Compounding this, `_mts_tick` relied on a background CSV loader (`spread_loader.py`) for far-month prices. If the external script writing the CSV crashed or stalled, MTS was entirely blinded to real-time far-month price action.

## Architectural Changes & Solutions

### 1. Robust Emergency Close & Stale Order Monitor
*   **Memory Reset:** `_process_manual_trade_flag` now explicitly extracts the strategy instance from the registry and calls `_reset()`.
*   **Log Completion:** Explicitly calls `_append_fill` to ensure `mts_trade_fills.jsonl` records the emergency exit.
*   **Stale Order Protection:** Added `_check_stale_mts_orders()` to `monitor.py`. If an `MTS_RELEASE` or `MTS_EXIT` order sits pending for > 30 seconds, it is automatically cancelled and marked for resubmission, ensuring limit orders don't hang during fast market movements.

### 2. Elimination of Hardcoded Tickers
*   **Context Injection:** Modified all `StrategyContext` creation sites in `monitor.py` to strictly inject `ticker=self.ticker`.
*   **Strategy Refactoring:** `tmf_spread.py` `init()` now prioritizes `context.market.ticker`, falling back to `UNKNOWN` rather than `MXF`.
*   **UI Dynamic Scaling:** The dashboard now defines a global `_TICKER = futures_cfg.get("ticker")` and utilizes `get_point_value()` for all PnL calculations, replacing all hardcoded `* 50` math. The 'Ticker' column was also restored to the orders table.

### 3. Real-Time Intra-Bar Execution (The "Tick-Level" Upgrade)
*   **Unblocked Event Loop:** Extracted the MTS execution block from the `elif` branch in `on_tick`. `_mts_tick` is now invoked unconditionally on *every single tick*.
*   **Real-Time Price Override:** `monitor.py` now directly injects the live websocket price into the bar dictionary passed to `_mts_tick`:
    ```python
    _rt_bar["near_close_rt"] = price
    _rt_bar["near_high_rt"] = bar["high"]
    _rt_bar["near_low_rt"] = bar["low"]
    _rt_bar["far_close_rt"] = self._far_current_bar["close"]
    _rt_bar["far_high_rt"] = self._far_current_bar["high"]
    _rt_bar["far_low_rt"] = self._far_current_bar["low"]
    ```
*   **Extreme Value Evaluation:** `_manage_position` in `tmf_spread.py` no longer looks at `close` for trailing stop decisions. It now actively updates `self._peak` using `far_high` and calculates retracement using `far_low` (for long positions). This ensures that intra-bar flash spikes push the stop-loss up, and intra-bar flash crashes immediately trigger the exit, regardless of the 5-minute or CSV state.

### 4. Parameter Hot-Reloading
*   Added logic to `on_bar` in `tmf_spread.py` to hot-reload configuration parameters (`atr_multiplier_stop`, `atr_multiplier_trail`) on every tick. This allows developers to tune MTS sensitivity from the dashboard and have it apply instantly to active positions without a system restart.

## Verification
All changes were validated through surgical unit tests, including an end-to-end stream simulation (`test_intra_bar_extremes`) that confirmed intra-bar peaks are captured and trailing stops are executed correctly based on tick-level data.
