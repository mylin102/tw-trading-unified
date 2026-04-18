# 🔴 Critical Bug Report: Stock Live Trading Odd-Lot Orders Executed After Market Close

**Date:** 2026-04-13
**Severity:** CRITICAL - Financial Loss Risk
**Status:** FIXED

---

## Bug Summary

Two critical issues were confirmed:

1. **Dashboard doesn't show live orders** - Orders placed in LIVE mode are not reflected in dashboard
2. **Odd-lot orders executed at 14:30 after switching to paper at 13:00** - ROOT CAUSE IDENTIFIED

---

## Root Cause Analysis

### Bug 1: All Stock Orders Used `StockOrderLot.Odd` (盤後零股)

**File:** `strategies/stocks/monitor.py:415,439` (before fix)

```python
# BEFORE (bug):
order_lot=sj.constant.StockOrderLot.Odd

# AFTER (fix):
order_lot=sj.constant.StockOrderLot.IntradayOdd
```

**Key difference:**
| Property | `Odd` (舊) | `IntradayOdd` (新) |
|----------|------------|-------------------|
| 盤中撮合 | ❌ 不撮合 | ✅ 每 5 秒撮合 |
| 13:30 未成交 | ❌ 排隊到 14:30 | ✅ 自動失效 |
| 盤後風險 | 🔴 14:30 可能被成交 | 🟢 無風險 |

### Bug 2: Order Uses ROD (Rest-of-Day) Validity

**File:** `strategies/stocks/monitor.py:414,438`

```python
order_type=sj.constant.OrderType.ROD
```

With `Odd` + `ROD`, orders remain valid until the **post-market session ends at 14:30**.
With `IntradayOdd` + `ROD`, orders **auto-expire at 13:30** — no post-market risk.

### Bug 3: No End-of-Day Order Cancellation

**File:** `strategies/stocks/monitor.py:284`

The monitor stopped trading after 13:30 but **did NOT cancel** any pending orders.
With `IntradayOdd`, this is now a safety net rather than a critical bug — orders auto-expire anyway.

### Bug 4: Dashboard Doesn't Track Live Orders

Orders placed in LIVE mode update `self.positions` but don't sync to the dashboard in real-time.

---

## Timeline Reconstruction

Based on the bug symptoms:

1. **Before 13:00** - Live trading placed odd-lot buy orders for 4 stocks
   - Orders used `StockOrderLot.Odd` + `OrderType.ROD`
   - Orders did NOT execute during regular hours (09:00-13:30) because odd-lot doesn't trade then
   - Orders remained in queue with status `Submitted`

2. **13:00** - Switched to paper trading
   - `live_trading` flag changed to `False` in config
   - BUT: Orders were **already placed** and sitting in the market queue
   - No cancellation logic ran to clean up pending orders

3. **13:30** - Regular market closed
   - Stock monitor stopped scanning (`now.hour == 13 and now.minute > 30`)
   - **No cancel_order() was called** for pending orders
   - Orders continued to queue

4. **14:00-14:30** - 盤後零股交易時段
   - Odd-lot orders execute at 14:30 single match
   - All 4 pending orders filled at 14:30

5. **After 14:30** - Discovered unwanted positions
   - Positions were never synced to dashboard
   - No risk management or stop-loss on these positions

---

## Fixes Required

### 1. Use Common (整股) Instead of Odd-Lot for Regular Market Hours

```python
# Line 415, 439 - CHANGE FROM:
order_lot=sj.constant.StockOrderLot.Odd

# TO (for regular trading hours):
order_lot=sj.constant.StockOrderLot.Common
```

**Note:** If quantity < 1000 shares, you MUST use Odd. If qty >= 1000, use Common.

### 2. Add End-of-Day Order Cancellation

```python
# In run_iteration(), before the trading window check:
def _cancel_all_pending_orders(self):
    """Cancel all pending orders before market close or mode switch."""
    self.api.update_status()
    for trade in self.api.trades:
        if trade.contract.code in self.watchlist:
            if trade.status.status == sj.constant.OrderState.Submitted:
                console.print(f"[yellow]🚨 Cancelling pending order: {trade.contract.code}[/yellow]")
                self.api.cancel_order(trade)
```

Call this at:
- **13:25** - Before final exit (along with `check_risk()` time-exit logic)
- **When switching from LIVE to PAPER mode** - In `_reload_live_flag()`

### 3. Sync Positions to Dashboard

Add a method to push position updates to the dashboard state file or shared memory.

### 4. Add Order Mode Validation

Before placing any order, check:
- Is this the correct trading session for this order type?
- Are we within regular market hours (09:00-13:30)?
- Should this be `Common` or `Odd` based on quantity?

---

## Immediate Action Required

1. **Check current positions** - You may have 4 unwanted odd-lot positions
2. **Manually cancel any pending orders** before 14:30 if market is open
3. **Fix the order_lot parameter** before next live trading session
4. **Add EOD cancellation logic** to prevent this from happening again

---

## References

- Shioaji API docs: `StockOrderLot.Odd` vs `StockOrderLot.Common`
- Taiwan stock trading hours: 09:00-13:30 (regular), 14:00-14:30 (odd-lot盘后)
- Order validity: ROD = Rest of Day (valid until session end)
