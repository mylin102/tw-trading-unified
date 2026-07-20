# MTS Real Trading Mode Transition Plan

**Document Type:** Implementation Plan  
**Priority:** P0 — Live Trading Safety  
**Status:** Proposed  
**Target System:** `tw-trading-unified` / MTS Calendar Spread  
**Primary Component:** `strategies/futures/monitor.py`  
**Related Components:** `OrderManager`, `PaperTrader`, broker adapter, lifecycle state, fills ledger, runtime state, PM2 startup flow

---

## 1. Objective

建立一套明確、可驗證、預設拒絕的 Paper Trading → Real Trading 切換流程，避免僅修改：

```yaml
live_trading: true
```

並重啟程式後，系統在未知或不一致狀態下向真實券商送出委託。

此計畫的核心目標為：

> 只有在 Paper execution context 已完全收斂、真實券商帳戶狀態已確認安全、Live state 已獨立初始化且 reconciliation 通過時，系統才允許進入 Real Trading。

任何檢查失敗時，系統必須：

```text
Fail Closed
```

也就是：

- 不切換至 Real Trading
- 不送出任何真實委託
- 不自動平掉真實帳戶未知部位
- 不沿用 Paper lifecycle
- 明確回報阻擋原因與修復指引

---

## 2. Problem Statement

目前 `live_trading` 是啟動時讀取的設定值：

```python
self.live_trading = self.cfg.get("live_trading", False)
```

並用於初始化 `OrderManager`：

```python
_om_mode = "live" if self.live_trading else "paper"
self.order_mgr = OrderManager(
    mode=_om_mode,
    broker_adapter=broker,
)
```

但目前存在以下結構性風險。

### 2.1 PaperTrader 狀態與 Live execution 不一致

目前即使進入 live mode，仍可能初始化：

```python
self.trader = PaperTrader(initial_balance=...)
```

重啟後 `PaperTrader` 從初始狀態開始，無法代表真實券商的：

- 實際持倉
- 平均成本
- 可用保證金
- 未成交委託
- 成交回報
- 舊有真實部位

### 2.2 Lifecycle 重啟後可能回到 FLAT

```python
self._lifecycle_oca = PositionLifecycle()
```

可能使本地 lifecycle 回到 `FLAT`，但：

- Paper runtime state 可能仍記錄未完成交易
- 真實券商帳戶可能有既有部位
- 本地 fills ledger 可能仍存在 active trade
- pending order callback 可能尚未完成

### 2.3 Config flag 不是安全的 mode transition mechanism

僅修改：

```yaml
live_trading: true
```

並不能保證：

```text
Paper position = 0
Paper lifecycle = FLAT
Paper pending orders = 0
Broker position = 0
Broker open orders = 0
Live local state = clean
Broker reconciliation = PASSED
```

因此 `live_trading` 不應直接代表「已允許送真單」，而只能代表：

```text
Requested Execution Mode = LIVE
```

真正可送真單仍必須由 startup gate 與 reconciliation gate 決定。

---

## 3. Safety Policy

Real Trading 的核心不變量定義如下：

```text
LIVE_ORDER_ALLOWED
IFF
requested_mode == LIVE
AND transition_state == LIVE_READY
AND paper_context == DRAINED
AND broker_connection == HEALTHY
AND broker_reconciliation == PASSED
AND live_state_namespace == VALID
AND lifecycle_matches_broker_position
AND pending_orders_are_reconciled
```

只要任一條件未滿足：

```text
LIVE_ORDER_ALLOWED = False
```

所有下列 action 均必須被阻擋：

- ENTRY
- RELEASE
- TRAIL
- PROFIT_EXIT
- TIMEOUT_EXIT
- SETTLEMENT_EXIT
- MANUAL strategy exit
- 自動 emergency flatten

例外只能是明確、人工確認、指定合約與指定數量的 broker-level operator action，且不得透過策略 lifecycle 自動推導。

---

## 4. Scope

### 4.1 In Scope

本計畫涵蓋：

1. Paper → Live mode transition FSM
2. Paper execution draining
3. Paper position/lifecycle/order/callback 完整檢查
4. Broker position 與 open order 查詢
5. Live state namespace 隔離
6. Startup reconciliation
7. Live order hard gate
8. Fail-closed 錯誤回報
9. PM2 restart 後的重複驗證
10. 測試、事故注入與驗收標準
11. Audit log 與 transition manifest

### 4.2 Out of Scope

本階段不處理：

- 自動接管既有真實部位
- 自動將 broker position 推導成 MTS spread lifecycle
- 自動平掉未知真實部位
- Paper position 與 Real position 的一對一遷移
- 跨帳戶 position migration
- 多 broker 帳戶同時運作
- 模擬成交紀錄轉換成真實成本基礎

如果 Real broker 已有持倉，預設行為是：

```text
LIVE_QUARANTINED
```

而不是自動 reconcile 成可交易狀態。

---

## 5. Proposed Architecture

## 5.1 Separate Requested Mode From Effective Mode

新增兩個概念：

```python
requested_mode: Literal["paper", "live"]
effective_mode: Literal[
    "paper",
    "paper_draining",
    "live_preflight",
    "live_quarantined",
    "live_ready",
]
```

設定檔只決定：

```python
requested_mode = "live"
```

但只有 transition 完成後：

```python
effective_mode = "live_ready"
```

才允許真實委託。

### Recommended Enum

```python
from enum import Enum

class ExecutionMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class ModeTransitionState(str, Enum):
    PAPER_ACTIVE = "paper_active"
    PAPER_DRAINING = "paper_draining"
    PAPER_DRAINED = "paper_drained"
    LIVE_PREFLIGHT = "live_preflight"
    LIVE_RECONCILING = "live_reconciling"
    LIVE_QUARANTINED = "live_quarantined"
    LIVE_READY = "live_ready"
    TRANSITION_BLOCKED = "transition_blocked"
```

---

## 5.2 Mode Transition FSM

```text
PAPER_ACTIVE
    |
    | switch_to_live requested
    v
PAPER_DRAINING
    |
    | paper position=0
    | lifecycle=FLAT
    | pending orders=0
    | inflight callbacks=0
    | active trade_id=None
    v
PAPER_DRAINED
    |
    | broker connected
    v
LIVE_PREFLIGHT
    |
    | broker positions queried
    | broker open orders queried
    | account identity validated
    | live state namespace validated
    v
LIVE_RECONCILING
    |
    +---- mismatch/error ----> LIVE_QUARANTINED
    |
    +---- all checks pass ---> LIVE_READY
```

任一步驟失敗：

```text
TRANSITION_BLOCKED
```

並保持：

```text
effective_mode != live_ready
live_order_allowed = False
```

---

## 6. Paper Draining Design

切換至 Real 前，不應直接將 `live_trading` 改為 `True` 並重啟。

系統必須先進入：

```text
PAPER_DRAINING
```

### 6.1 Drain Sequence

1. 阻擋所有新的 Paper ENTRY
2. 停止新的策略 lifecycle 建立
3. 取消所有 Paper pending ENTRY orders
4. 依既有策略規則平掉 Paper position
5. 等待 Paper fill callback 完成
6. 等待 lifecycle 收斂至 `FLAT`
7. 驗證 active trade 已關閉
8. 寫入 paper-drained checkpoint

### 6.2 Paper Drain Completion Conditions

Paper 只有在下列條件全部成立時才能視為 drained：

```python
def is_paper_drained(snapshot) -> bool:
    return (
        snapshot.position_qty == 0
        and snapshot.lifecycle_phase == "FLAT"
        and snapshot.pending_order_count == 0
        and snapshot.inflight_callback_count == 0
        and snapshot.active_trade_id is None
        and snapshot.unresolved_fill_count == 0
        and snapshot.pending_action is None
    )
```

### 6.3 Important Constraint

「呼叫 flatten」不等於「已完成平倉」。

以下狀態仍不可切換：

```text
EXIT_SUBMITTED
EXIT_PARTIALLY_FILLED
CALLBACK_PENDING
LIFECYCLE_EXITING
ORDER_CANCEL_PENDING
```

必須等待 terminal condition：

```text
position=0
AND lifecycle=FLAT
AND pending_orders=0
AND inflight_callbacks=0
```

---

## 7. Real Broker Preflight

Paper drained 後，系統才可以連線並檢查真實券商帳戶。

### 7.1 Required Broker Checks

至少檢查：

| Check | Required Result |
|---|---|
| Broker connection | Connected and authenticated |
| Account identity | Matches configured account hash |
| MTS contract positions | Empty |
| Relevant futures positions | Empty or explicitly allowed |
| Open orders | Empty |
| Partially filled orders | Empty |
| Cancel-pending orders | Empty |
| Broker API health | Healthy |
| Position timestamp | Fresh |
| Order snapshot timestamp | Fresh |

### 7.2 Default Safety Rule

最簡單且最安全的第一版政策：

```text
Real broker 必須完全無 MTS 相關持倉
AND
Real broker 必須無任何 MTS 相關 open order
```

若存在任何部位：

```text
LIVE_QUARANTINED
Reason = BROKER_POSITION_NOT_FLAT
```

若存在任何未成交委託：

```text
LIVE_QUARANTINED
Reason = BROKER_OPEN_ORDERS_EXIST
```

### 7.3 No Automatic Broker Flatten

系統不得因切換失敗而自動送出真實平倉單。

原因：

- 無法確定既有部位屬於哪個策略
- 無法確定部位是否為人工交易
- 無法確定該部位是否為 hedge
- 無法確定 trade_id 與 lifecycle
- 自動平倉本身可能造成新風險

---

## 8. State Namespace Isolation

Paper 與 Live 不得共用相同 state path。

### 8.1 Proposed Directory Layout

```text
exports/trades/
├── paper/
│   ├── runtime_status.json
│   ├── mts_position_state.json
│   ├── mts_trade_fills.jsonl
│   ├── orders.json
│   ├── lifecycle_state.json
│   └── transition_checkpoints/
│
└── live/
    ├── runtime_status.json
    ├── mts_position_state.json
    ├── mts_trade_fills.jsonl
    ├── orders.json
    ├── lifecycle_state.json
    ├── reconciliation_report.json
    └── transition_checkpoints/
```

### 8.2 Required State Metadata

每份 state file 必須包含：

```json
{
  "execution_mode": "paper",
  "account_id_hash": null,
  "session_id": "PAPER-20260717-...",
  "process_start_id": "...",
  "schema_version": "1.0",
  "config_hash": "...",
  "updated_at": "2026-07-17T..."
}
```

Live state：

```json
{
  "execution_mode": "live",
  "account_id_hash": "sha256:...",
  "session_id": "LIVE-20260717-...",
  "process_start_id": "...",
  "schema_version": "1.0",
  "config_hash": "...",
  "updated_at": "2026-07-17T..."
}
```

### 8.3 Hard Guard

Live mode 讀到 Paper state 時：

```text
STATE_MODE_MISMATCH
```

必須拒絕載入。

```python
if state.execution_mode != requested_mode:
    raise ModeStateMismatchError(...)
```

不得嘗試「相容性恢復」。

---

## 9. Live Execution Context

建立不可混淆的 execution context。

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class ExecutionContext:
    requested_mode: Literal["paper", "live"]
    effective_mode: str
    account_id_hash: str | None
    session_id: str
    process_start_id: str
    config_hash: str
    state_namespace: str
```

以下元件都必須綁定同一個 `ExecutionContext`：

- `OrderManager`
- `PositionProvider`
- `StateStore`
- `FillsLedger`
- `PositionLifecycle`
- `RiskEngine`
- `BrokerAdapter`
- `RuntimeStatusWriter`

若 mode、namespace、account 不一致，啟動失敗。

---

## 10. Position Authority Separation

Live mode 不應使用 `PaperTrader` 作為 position authority。

### 10.1 Proposed Interface

```python
class PositionProvider:
    def get_positions(self):
        raise NotImplementedError

    def get_open_orders(self):
        raise NotImplementedError


class PaperPositionProvider(PositionProvider):
    ...


class BrokerPositionProvider(PositionProvider):
    ...
```

### 10.2 Mode Binding

```text
Paper mode:
Position authority = PaperPositionProvider

Live mode:
Position authority = BrokerPositionProvider
```

`PaperTrader` 可保留作為：

- Paper execution simulator
- Paper PnL calculator
- Research tool

但不可在 Live mode 中作為真實 position source。

---

## 11. Live Startup Reconciliation

### 11.1 Reconciliation Inputs

Live startup 至少取得：

```text
broker_positions
broker_open_orders
live_local_state
live_fills_ledger
live_lifecycle_state
configured_account_identity
```

### 11.2 Initial V1 Policy

第一版只允許：

```text
broker flat
AND live local flat
AND no open orders
AND no active live trade
```

也就是 clean-start-only。

### 11.3 Reconciliation Decision

```python
def reconcile_live_startup(
    broker_positions,
    broker_orders,
    local_state,
    ledger_state,
    lifecycle_state,
):
    if broker_positions:
        return quarantine("BROKER_POSITION_NOT_FLAT")

    if broker_orders:
        return quarantine("BROKER_OPEN_ORDERS_EXIST")

    if local_state.has_position:
        return quarantine("LIVE_LOCAL_STATE_NOT_FLAT")

    if ledger_state.has_active_trade:
        return quarantine("LIVE_LEDGER_HAS_ACTIVE_TRADE")

    if lifecycle_state.phase != "FLAT":
        return quarantine("LIVE_LIFECYCLE_NOT_FLAT")

    return ready()
```

---

## 12. Live Order Hard Gate

即使 transition service 有 bug，最終送單路徑仍必須有最後一道 hard gate。

### 12.1 Central Gate

```python
def assert_live_order_allowed(context, reconciliation):
    if context.requested_mode != "live":
        return

    if context.effective_mode != "live_ready":
        raise LiveOrderBlocked(
            reason="LIVE_MODE_NOT_READY"
        )

    if not reconciliation.passed:
        raise LiveOrderBlocked(
            reason="BROKER_RECONCILIATION_NOT_PASSED"
        )
```

### 12.2 Required Injection Points

至少在以下位置加入 hard gate：

- `_submit_mts_order_signal()`
- release order submission
- single-leg trail exit
- profit exit
- timeout exit
- settlement exit
- emergency flatten
- manual strategy-triggered exit
- generic `OrderManager.submit_order()`

最後一層應位於 `OrderManager` 或 broker adapter 前，避免上層遺漏。

---

## 13. Proposed Service API

不建議由使用者直接編輯 config 並重啟。

新增明確 API：

```python
result = service.switch_to_live()
```

### 13.1 Return Model

```python
@dataclass
class ModeTransitionResult:
    approved: bool
    previous_mode: str
    requested_mode: str
    effective_mode: str
    transition_id: str
    failed_checks: list[str]
    warnings: list[str]
    evidence: dict
```

### 13.2 Example Success

```json
{
  "approved": true,
  "previous_mode": "paper",
  "requested_mode": "live",
  "effective_mode": "live_ready",
  "transition_id": "MODE-20260717-000001",
  "failed_checks": [],
  "warnings": [],
  "evidence": {
    "paper_position_qty": 0,
    "paper_lifecycle": "FLAT",
    "paper_pending_orders": 0,
    "broker_positions": 0,
    "broker_open_orders": 0,
    "reconciliation": "PASSED"
  }
}
```

### 13.3 Example Failure

```json
{
  "approved": false,
  "previous_mode": "paper",
  "requested_mode": "live",
  "effective_mode": "transition_blocked",
  "transition_id": "MODE-20260717-000002",
  "failed_checks": [
    "PAPER_LIFECYCLE_NOT_FLAT",
    "BROKER_OPEN_ORDERS_EXIST"
  ],
  "warnings": [],
  "evidence": {
    "paper_lifecycle": "ARMED",
    "paper_trade_id": "MTS-20260717-001",
    "broker_open_orders": 1
  }
}
```

---

## 14. User-Facing Status and Error Messages

### 14.1 Required Output Format

```text
Switch to LIVE requested

Paper checks
[✓] Position quantity = 0
[✓] Lifecycle = FLAT
[✓] Pending orders = 0
[✓] Inflight callbacks = 0

Broker checks
[✓] Connection = HEALTHY
[✗] Open orders = 1
    TMFU6 BUY 2, status=Submitted

Live checks
[✓] Live namespace = valid
[✓] Live lifecycle = FLAT

Result
LIVE transition BLOCKED

Reason
BROKER_OPEN_ORDERS_EXIST

Action
Cancel or resolve the broker order, then run the transition again.
```

### 14.2 Machine-Readable Reason Codes

至少定義：

```text
PAPER_POSITION_NOT_FLAT
PAPER_LIFECYCLE_NOT_FLAT
PAPER_PENDING_ORDERS_EXIST
PAPER_CALLBACKS_INFLIGHT
PAPER_ACTIVE_TRADE_EXISTS
PAPER_LEDGER_UNRESOLVED

BROKER_NOT_CONNECTED
BROKER_AUTH_FAILED
BROKER_ACCOUNT_MISMATCH
BROKER_POSITION_NOT_FLAT
BROKER_OPEN_ORDERS_EXIST
BROKER_SNAPSHOT_STALE

LIVE_STATE_MODE_MISMATCH
LIVE_STATE_NOT_CLEAN
LIVE_LEDGER_HAS_ACTIVE_TRADE
LIVE_LIFECYCLE_NOT_FLAT
LIVE_NAMESPACE_INVALID

RECONCILIATION_FAILED
TRANSITION_TIMEOUT
TRANSITION_INTERNAL_ERROR
```

---

## 15. Config Changes

### 15.1 Deprecate Direct `live_trading` Activation

原本：

```yaml
live_trading: true
```

建議改為：

```yaml
execution:
  requested_mode: live

  live_transition:
    require_paper_drain: true
    require_broker_flat: true
    require_no_open_orders: true
    require_clean_live_state: true
    fail_closed: true
```

### 15.2 Runtime Effective Mode

Runtime status 必須輸出：

```json
{
  "requested_mode": "live",
  "effective_mode": "live_quarantined",
  "live_order_allowed": false,
  "transition_state": "LIVE_QUARANTINED",
  "transition_block_reason": "BROKER_POSITION_NOT_FLAT"
}
```

---

## 16. PM2 Startup Behavior

### 16.1 Startup With Requested Paper

```text
requested_mode=paper
→ load paper namespace
→ initialize PaperPositionProvider
→ effective_mode=paper
```

### 16.2 Startup With Requested Live

```text
requested_mode=live
→ do not immediately enable broker order submission
→ effective_mode=live_preflight
→ load live namespace only
→ connect broker
→ query broker position/order snapshots
→ run reconciliation
→ pass: live_ready
→ fail: live_quarantined
```

### 16.3 Restart Safety

PM2 restart 後不可沿用記憶體中的：

- `live_order_allowed=True`
- reconciliation passed flag
- lifecycle readiness

每次 process start 都必須重新執行 broker reconciliation。

---

## 17. Auditability

每次 mode transition 建立一份 append-only audit record。

### 17.1 Transition Manifest

```text
exports/trades/mode_transitions/
└── MODE-20260717-000001.json
```

### 17.2 Required Fields

```json
{
  "transition_id": "MODE-20260717-000001",
  "requested_at": "...",
  "completed_at": "...",
  "from_mode": "paper",
  "requested_mode": "live",
  "effective_mode": "live_ready",
  "approved": true,
  "config_hash": "...",
  "git_commit": "...",
  "process_start_id": "...",
  "paper_snapshot": {},
  "broker_snapshot": {},
  "live_snapshot": {},
  "checks": [],
  "failed_checks": []
}
```

不得只寫 summary log，必須保留完整 evidence。

---

## 18. Implementation Phases

## Phase 1 — Mode Model and Hard Gate

### Deliverables

- `ExecutionMode`
- `ModeTransitionState`
- `ExecutionContext`
- `live_order_allowed` central invariant
- Generic order submission hard gate
- Runtime status fields

### Acceptance Criteria

- `requested_mode=live` 不等於可以送真單
- effective mode 非 `live_ready` 時，所有 broker orders 被拒絕
- 單元測試涵蓋 ENTRY/RELEASE/TRAIL/EXIT

---

## Phase 2 — Paper Drain

### Deliverables

- `PAPER_DRAINING`
- 新 ENTRY guard
- pending order cancellation
- callback drain tracking
- paper drained checkpoint
- drain timeout 與 reason codes

### Acceptance Criteria

- Paper 尚有 position 時不可進 Live preflight
- lifecycle 非 FLAT 時不可切換
- pending order 或 callback 尚未完成時不可切換
- drain 完成後 evidence 可重現

---

## Phase 3 — State Namespace Isolation

### Deliverables

- `paper/` 與 `live/` 分離路徑
- state execution mode metadata
- mode mismatch hard failure
- ledger/order/lifecycle 分區
- migration script 或初始化工具

### Acceptance Criteria

- Live 不會讀取 Paper state
- Paper 不會讀取 Live state
- path、mode、account metadata 不一致時啟動失敗
- 舊 shared state 不可被默默採用

---

## Phase 4 — Broker Preflight and Reconciliation

### Deliverables

- broker position snapshot
- broker open order snapshot
- account identity validation
- freshness validation
- clean-start reconciliation
- `LIVE_QUARANTINED`

### Acceptance Criteria

- broker 有部位時切換失敗
- broker 有 open order 時切換失敗
- broker API timeout 時切換失敗
- stale snapshot 時切換失敗
- 所有條件通過才進入 `LIVE_READY`

---

## Phase 5 — Transition Service and UX

### Deliverables

- `service.switch_to_live()`
- structured result
- CLI command
- Dashboard control
- actionable failure messages
- transition audit manifest

### Acceptance Criteria

- 使用者能看到每一項檢查
- 同時顯示 human-readable 與 machine-readable result
- 失敗時保持原 execution mode
- 重試 transition 不會產生重複副作用

---

## Phase 6 — Restart and Recovery Validation

### Deliverables

- PM2 restart scenario tests
- broker disconnect/reconnect tests
- callback-late-arrival tests
- process crash during transition tests
- transition checkpoint recovery

### Acceptance Criteria

- transition 中途 crash 後不會直接進入 Live
- PM2 restart 必須重新 reconciliation
- 舊 callback 不可污染新的 Live session
- transition ID 與 session ID 可追蹤

---

## 19. Test Plan

## 19.1 Unit Tests

### Mode Gate

- requested live + effective preflight → order blocked
- requested live + quarantined → order blocked
- requested live + ready + reconcile passed → order allowed
- paper mode → paper order path unchanged

### Paper Drain

- position nonzero → blocked
- lifecycle ARMED → blocked
- pending order exists → blocked
- callback inflight → blocked
- active trade ID exists → blocked
- all clear → drained

### Broker Preflight

- broker disconnected → blocked
- account mismatch → blocked
- position exists → quarantined
- open order exists → quarantined
- stale snapshot → quarantined
- clean snapshot → pass

### State Isolation

- live reads paper file → error
- paper reads live file → error
- account hash mismatch → error
- namespace missing → explicit initialization only

---

## 19.2 Integration Tests

1. Paper 有 spread → request live  
   Expected: Paper enters draining, Live blocked until fully flat.

2. Paper flat，Broker flat  
   Expected: transition approved, `LIVE_READY`.

3. Paper flat，Broker 有 position  
   Expected: `LIVE_QUARANTINED`, no broker order sent.

4. Paper flat，Broker 有 open order  
   Expected: blocked.

5. Paper EXIT submitted but callback not returned  
   Expected: blocked.

6. Transition during PM2 restart  
   Expected: restart returns to preflight/quarantine, never assumes ready.

7. Broker snapshot timeout  
   Expected: fail closed.

8. Live state contains active trade but broker flat  
   Expected: quarantine.

9. Paper state accidentally located in live directory  
   Expected: mode mismatch failure.

10. Late Paper callback arrives after Live session initialized  
    Expected: rejected due to session/mode mismatch.

---

## 19.3 Fault Injection Tests

- Broker network disconnect during preflight
- Broker API returns partial positions
- Order snapshot older than allowed threshold
- State file truncated
- Fills ledger malformed
- Duplicate transition request
- Transition service called concurrently
- Process killed after paper drained but before broker reconcile
- Process killed after broker reconcile but before `LIVE_READY` commit
- Broker reconnect returns different account

---

## 20. Concurrency and Idempotency

Mode transition 必須是 single-flight operation。

### 20.1 Lock

```text
mode_transition.lock
```

或 process-wide async lock。

### 20.2 Idempotency Key

```text
transition_id
```

相同 transition 不得：

- 重複 flatten
- 重複 cancel
- 重複初始化 live state
- 重複發送任何 broker order

### 20.3 Atomic Commit

`LIVE_READY` 必須在以下資料持久化成功後才設定：

```text
broker reconciliation report written
live state initialized
transition manifest written
runtime status updated
```

---

## 21. Observability

### Required Logs

```text
[MODE_TRANSITION_REQUESTED]
[PAPER_DRAIN_STARTED]
[PAPER_DRAIN_BLOCKED]
[PAPER_DRAIN_COMPLETED]
[LIVE_PREFLIGHT_STARTED]
[BROKER_SNAPSHOT_CAPTURED]
[LIVE_RECONCILIATION_PASSED]
[LIVE_RECONCILIATION_FAILED]
[LIVE_TRANSITION_BLOCKED]
[LIVE_READY]
[LIVE_ORDER_BLOCKED]
```

### Metrics

```text
mode_transition_attempt_total
mode_transition_success_total
mode_transition_blocked_total
mode_transition_duration_seconds
paper_drain_duration_seconds
broker_reconciliation_failure_total
live_order_blocked_total
```

---

## 22. Rollback Strategy

若 Live transition 通過後發現初始化錯誤：

```text
LIVE_READY
→ LIVE_QUARANTINED
```

立即：

- 禁止新 ENTRY
- 禁止策略自動 action
- 保留 broker position snapshot
- 不自動切回 Paper
- 不將真實部位匯入 PaperTrader
- 發出高優先級告警

只有 broker 確認 flat 且所有 live state 清理完成後，才允許回到：

```text
PAPER_ACTIVE
```

---

## 23. Definition of Done

本功能完成必須滿足：

- [ ] `live_trading=true` 不會直接允許真實送單
- [ ] Paper 未完全 drained 時無法切換
- [ ] Broker 有持倉時無法切換
- [ ] Broker 有未成交委託時無法切換
- [ ] Broker 無法連線時無法切換
- [ ] Paper/Live state 完全隔離
- [ ] Live mode 不使用 PaperTrader 作為 position authority
- [ ] PM2 每次重啟都重新 reconciliation
- [ ] 所有 broker order path 有 central hard gate
- [ ] transition 失敗時提供明確原因與 evidence
- [ ] transition audit manifest 可追溯
- [ ] concurrency、crash、late callback 測試通過
- [ ] 不會自動平掉未知真實部位
- [ ] 不會把 Paper lifecycle 帶入 Live
- [ ] 所有 P0 測試通過

---

## 24. Recommended Initial Delivery Boundary

為降低第一版複雜度，建議 V1 採用：

```text
Clean Start Only
```

也就是 Real Trading 只在以下情境啟用：

```text
Paper fully drained
Broker completely flat
Broker has no open orders
Live local state clean
Live lifecycle FLAT
Live ledger has no active trade
Reconciliation passed
```

暫不支援：

```text
Adopt Existing Broker Position
```

這能顯著降低 lifecycle reconstruction、trade attribution 與 orphan position 的風險。

---

## 25. Final Safety Invariant

正式固化以下 invariant：

```text
No Real Order Before Reconciliation.
No Mode Switch With Unknown Position.
No Paper State in Live Execution.
No Automatic Adoption of Existing Broker Positions.
No Warning-Only Failure: All Safety Failures Block.
```

對應程式語義：

```python
assert not real_order_submitted unless (
    execution_context.requested_mode == "live"
    and execution_context.effective_mode == "live_ready"
    and transition_result.approved
    and broker_reconciliation.passed
    and live_state.execution_mode == "live"
    and live_state.account_id_hash == broker.account_id_hash
)
```
