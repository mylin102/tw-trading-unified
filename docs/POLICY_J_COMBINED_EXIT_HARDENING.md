# Policy J Combined Exit Execution Hardening & Prevention Architecture

## Overview
This document specifies the mandatory architectural invariants and bug preventions for **Policy J Combined Exit** (simultaneous dual-leg trailing exit from `SPREAD` phase).

---

## 1. Identified Runtime Defects & Root Causes

### Defect 1: Order Persistence Omission (`exports/trades/*_orders.json`)
* **Symptom**: When Policy J triggered `COMBINED_EXIT`, the Order Status panel listed Entry orders (`ORD-000011` / `ORD-000012`) but omitted Exit orders.
* **Root Cause**: `strategies/futures/monitor.py` line 3941 created memory orders (`_near_order` and `_far_order`) and called `self.order_mgr.submit()`, but omitted `self._save_orders_file_wrapper()`. When market closed immediately after submission (04:59:59), no subsequent ticks arrived to flush the orders to JSON disk storage.
* **Fix**: Added immediate `self._save_orders_file_wrapper()` call in the `COMBINED_EXIT` block after submitting both legs.

### Defect 2: Peak PnL Memory Erasure Across Restarts
* **Symptom**: After restart recovery, positions with Peak PnL $\ge 200$ TWD failed to trigger `COMBINED_EXIT` at open, falling back to single-leg `RELEASE` stop.
* **Root Cause**: `strategies/plugins/futures/active/tmf_spread.py` line 2220 restored state from `/tmp/mts_position_state.json` but omitted `self._peak_net_exit_pnl_twd`, resetting peak profit tracking to `0.0`.
* **Fix**: Added `self._peak_net_exit_pnl_twd = float(state.get("peak_net_exit_pnl_twd") or 0.0)` during recovery in `_restore_position_state()`.

---

## 2. Mandatory Architectural Invariants

1. **Immediate Order Disk Persistence**:
   Every order submission for `COMBINED_EXIT` (`COMBINED_EXIT_NEAR` & `COMBINED_EXIT_FAR`) MUST invoke `self._save_orders_file_wrapper()` before returning from the tick handler.

2. **Peak PnL Memory Preservation**:
   `peak_net_exit_pnl_twd` MUST be written to `/tmp/mts_position_state.json` on every state write and MUST be restored during restart recovery (`_restore_position_state()`).

3. **Strict Trade ID & Execution Idempotency**:
   - Both Combined Exit legs MUST register `"trade_id": _trade_id` in `_pending_lifecycle_orders`.
   - Generic `"COMBINED_EXIT"` trade IDs are strictly forbidden and trigger a fail-closed `RuntimeError`.

---

## 3. Verification & Governance Checkpoints

- **Unit Tests**: `tests/strategies/test_policy_j_combined_exit_execution.py` (31/31 passed)
- **Runtime Deployment**: verified clean PM2 restart (`trading-system` PID 17024, `dashboard` PID 83590).
