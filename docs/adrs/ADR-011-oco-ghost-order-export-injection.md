# ADR-011: OCO Ghost Order Prevention — Export Injection vs Order Counter

**Status**: ACCEPTED
**Date**: 2026-07-07
**Author**: Hermes Agent + mylin102

## Context

PM2 restart 後，dashboard 出現幽靈 OCO 委託單：
- 相同 order ID（ORD-000003, ORD-000004）重複出現兩次
- 第一次：filled 狀態，正確方向（SELL near / BUY far）
- 第二次：submitted 狀態，stale/wrong 方向

懷疑來源可能是 OrderManager counter 重複或 export 層 injection。

## Investigation

### ORD-xxxxxx 產生邏輯 (`core/order_management/order_manager.py`)

```
same session:
  create_order() → _next_id 單調遞增，不重複

PM2 restart:
  OrderManager() 重新初始化
  _next_id = 1（無持久化）
  ORD 序號可重用

reindex_orders():
  只在 active/completed 已重建後有效
  作用是把 _next_id 推到 max(existing ORD) + 1
  max() 保證只向前不後退
```

Counter 行為正確，不是問題來源。

### 真正的重複來源

`_save_orders_file_wrapper()` 在 restart 時的執行順序：

```text
1. _save_orders_file_wrapper()  ← 先執行
   ├── order_mgr.get_completed() → 空
   ├── order_mgr.get_pending()   → 空
   ├── export_data               → 空
   └── OCO injection guard 找不到 duplicate
       → 注入 ORD-000003, ORD-000004（從 lifecycle.release_group）

2. _reconcile_paper_oco_orders()  ← 後執行
   └── 重建真實 order 到 active_orders（已太晚）
```

結果：
- order_mgr 是空的，duplicate guard 失效
- lifecycle.release_group 從 state file 恢復，status=SUBMITTED
- 舊的 near_order_id / far_order_id 被當成新 pending order 寫入 orders JSON

## Decision

三層防禦，在 export 層解決，不修改 counter 邏輯：

### 1. Reorder: reconcile BEFORE save

```python
# monitor.py:4722-4726
# Before: save → reconcile
# After:  reconcile → save
self._reconcile_paper_oco_orders(strategy)
self._save_orders_file_wrapper()
self._mts_release_orders_flushed = True
```

### 2. Belt-and-suspenders: completed_ids guard

```python
# monitor.py:1944-1957
_completed_ids = {o.order_id for o in self.order_mgr.completed}
# ...
if _oid in _completed_ids:
    continue  # 即使 export_data 漏掉，completed 有就不注入
```

### 3. Invariant tests

`tests/test_order_lifecycle/test_oco_ghost_order_invariant.py`:

| Test | 場景 |
|------|------|
| `test_duplicate_order_id_guard` | completed 有兩筆 OCO，lifecycle SUBMITTED → 不注入 |
| `test_no_duplicate_order_ids` | completed entry orders 與 OCO ID 重疊 → export 無重複 |
| `test_restart_scenario_reconcile_before_save` | empty order_mgr → reconcile → export → 不注入 ghost |

## Consequences

- `_next_id` 不需要持久化。counter 行為正確，不應修改。
- 任何 dashboard 看到 duplicate order ID 的問題，應**先檢查 export 層的 injection**，而非 counter。
- OCO lifecycle 狀態機（SUBMITTED → PARTIALLY_FILLED → CANCELING_SIBLING → SIBLING_CANCELED → SINGLE_LEG → FLAT）本身是正確的，問題在 export 時機。

## Related

- ADR-010: Broker-Level Release OCO
- `strategies/futures/monitor.py`: `_save_orders_file_wrapper`, `_reconcile_paper_oco_orders`
- `strategies/plugins/futures/active/tmf_spread.py`: `_write_mts_state` (UPL guard)
