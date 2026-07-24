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

即：

- 不切換至 Real Trading
- 不送出任何真實委託
- 不自動平掉真實帳戶未知部位
- 不沿用 Paper lifecycle
- 明確回報阻擋原因與修復指引

---

## 2. Core Problem

目前 `live_trading` 是啟動時讀取的設定值：

```python
self.live_trading = self.cfg.get("live_trading", False)
```

並用於初始化 `OrderManager`。但存在以下結構性風險：

### 2.1 PaperTrader 無法代表真實券商狀態
- 重啟後 PaperTrader 從初始值開始
- 無法反映 real broker 的實際持倉、平均成本、可用保證金、未成交委託

### 2.2 Lifecycle 重啟後回到 FLAT
- `self._lifecycle_oca = PositionLifecycle()` 可能使 lifecycle 回到 FLAT
- Paper runtime state 可能仍記錄未完成交易
- Paper fills ledger 可能仍有 active trade

### 2.3 Config flag 不是安全的切換機制
- `live_trading: true` 不代表 paper position=0、lifecycle=FLAT、broker flat
- 需由 startup gate 與 reconciliation gate 決定真正可否送單

---

## 3. 首批交付範圍 (Phase 1 + 2 + P0-D)

為最快形成可接受的安全邊界，第一版實作涵蓋：

```text
P0-A: Mode model          — requested_mode / effective_mode / live_order_allowed
P0-B: Central hard gate   — 所有 broker order path 的最終阻擋
P0-C: Paper drain FSM     — 禁止新 ENTRY、退出、等待 callback、驗證 terminal state
P0-D: Minimal preflight   — broker connection、account、position=0、open_orders=0
```

### 為什麼 P0-D 必須綁入首批交付

Phase 1+2 若缺少 broker preflight，存在邏輯缺口：

```
Paper drained + startup gate passed → LIVE_READY
                                        ↓
                                broker 有舊倉？
                                system 不知道
                                        ↓
                                live_order_allowed = True (錯誤)
```

P0-D 的 broker check 可以很輕量（連線、查詢 position 與 open order 各一次），但從架構上封住這個缺口。

---

## 4. Mode Model

### 4.1 Requested Mode vs Effective Mode

```python
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

設定檔只表達意圖：

```python
requested_mode = "live"   # 來自 config
```

但只有 transition 完成後：

```python
effective_mode = "live_ready"   # FSM 收斂結果
```

才允許真實委託。

### 4.2 Execution Context

```python
@dataclass(frozen=True)
class ExecutionContext:
    requested_mode: str
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

---

## 5. Mode Transition FSM

```text
PAPER_ACTIVE
    |
    | switch_to_live requested
    v
PAPER_DRAINING
    |
    | paper position=0, lifecycle=FLAT,
    | pending_orders=0, inflight_callbacks=0,
    | active_trade_id=None, unresolved_fills=0
    v
PAPER_DRAINED
    |
    | broker connected
    v
LIVE_PREFLIGHT
    |
    | broker positions=0, open_orders=0,
    | account verified, snapshot fresh
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

保持：

```text
effective_mode != live_ready
live_order_allowed = False
```

---

## 6. Live Order Allowed — 最小安全公式

```text
live_order_allowed =
    requested_mode == LIVE
    AND effective_mode == LIVE_READY
    AND paper_drain_passed
    AND broker_connected
    AND broker_account_verified
    AND broker_positions_empty
    AND broker_open_orders_empty
    AND startup_reconciliation_passed
```

只要任一條件為 false：

```text
live_order_allowed = False
```

所有 broker order 被阻擋。

---

## 7. 原子性提交順序 (Prepare → Commit-Point → Activate)

LIVE_READY 的寫入必須有明確的提交順序，避免 crash 後處於不一致狀態：

```text
Step 1: Capture broker snapshot
Step 2: Write reconciliation report
Step 3: Initialize/write live state
Step 4: Write transition manifest (status=PREPARED)
Step 5: Write live_ready_checkpoint (status=COMMITTED)  ← authoritative commit point
Step 6: Update manifest (status=COMMITTED)
Step 7: Set in-memory effective_mode=LIVE_READY
Step 8: Set live_order_allowed=True
```

真正的決定性提交點是第 5 步。第 6 步即使 crash，也不影響安全判斷。

`live_ready_checkpoint.json` 內容：

```json
{
  "transition_id": "MODE-20260720-000001",
  "process_start_id": "...",
  "account_id_hash": "sha256:...",
  "broker_snapshot_hash": "sha256:...",
  "reconciliation_hash": "sha256:...",
  "config_hash": "sha256:...",
  "status": "COMMITTED"
}
```

### Crash Recovery Matrix

| Crash 時點 | 重啟判定 |
|---|---|
| Step 1 前 | 無有效 transition，重新 preflight |
| Step 2–3 間 | orphan preparation data，quarantine 或清理後重跑 |
| Step 4 後、Step 5 前 | incomplete transition (PREPARED)，LIVE_QUARANTINED |
| Step 5 後、記憶體切換前 | 上次已提交，但仍須重新 broker reconciliation |
| LIVE_READY 後 | restart 不繼承 allow，重新 preflight |

核心原則：

```text
Memory state is never authoritative across restart.
```

### LIVE_READY_IS_PROCESS_LOCAL

> LIVE_READY 只對當前 process_start_id 有效。

PM2 重啟後 process_start_id 改變，因此舊 checkpoint 不能直接授權新 process 下單。
它只能作為 audit evidence，而不能作為 runtime authorization。

重啟時行為：

```text
checkpoint exists   → 證明上次 transition 曾完整提交
                    → 但仍須重新 broker reconciliation（broker 狀態可能已變化）
                    → process_start_id 不同 → 舊授權失效

checkpoint PREPARED → 不自動完成，進入 LIVE_QUARANTINED
```

---

## 8. 兩層 Hard Gate

### 8.1 策略層 Gate

在以下送出點攔截：

```text
ENTRY
RELEASE
TRAIL
PROFIT_EXIT
TIMEOUT
SETTLEMENT_EXIT
EMERGENCY_FLATTEN
```

### 8.2 OrderManager 最終 Gate

任何 live order 在進 broker adapter 前重新驗證完整 context，而非只檢查一個布林值：

```python
def assert_live_order_allowed(
    execution_context,
    transition_state,
    reconciliation_state,
) -> None:
    if execution_context.requested_mode != ExecutionMode.LIVE:
        return

    if execution_context.effective_mode != EffectiveMode.LIVE_READY:
        raise LiveOrderBlocked("EFFECTIVE_MODE_NOT_LIVE_READY")

    if not execution_context.live_order_allowed:
        raise LiveOrderBlocked("LIVE_ORDER_FLAG_FALSE")

    if transition_state.status != "COMMITTED":
        raise LiveOrderBlocked("TRANSITION_NOT_COMMITTED")

    if not reconciliation_state.passed:
        raise LiveOrderBlocked("RECONCILIATION_NOT_PASSED")
```

兩層各自獨立。策略層攔截邏輯錯誤，OrderManager 層攔截實作遺漏。任一層失效時另一層仍能擋。

---

## 9. Paper Drain

### 9.1 Drain Sequence

1. 阻擋所有新的 Paper ENTRY
2. 停止新的策略 lifecycle 建立
3. 取消所有 Paper pending ENTRY orders
4. 依既有策略規則平掉 Paper position
5. 等待 Paper fill callback 完成
6. 等待 lifecycle 收斂至 FLAT
7. 驗證 active trade 已關閉
8. 寫入 paper-drained checkpoint

### 9.2 完成條件

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

### 9.3 最容易遺漏的狀態

- 已送 EXIT，但尚未 callback
- order 已 cancel，但 cancel acknowledgment 尚未返回
- lifecycle 已暫時顯示 FLAT，但 ledger 尚未完成 close
- 舊 session callback 在切換後抵達

Callback 必須帶 execution_mode、session_id、transition_id。不屬於目前 execution context 的 callback 應拒絕 mutation。

---

## 10. Broker Preflight (P0-D)

Paper drained 後，連線並檢查真實券商。

### 10.1 最低檢查

| Check | Required Result |
|---|---|
| Broker connection | Connected and authenticated |
| Account identity | Matches configured account hash |
| MTS contract positions | Empty |
| Open orders | Empty |
| Position snapshot | Fresh |
| Order snapshot | Fresh |

### 10.2 預設安全規則

```text
Real broker 必須完全無 MTS 相關持倉
AND
Real broker 必須無任何 MTS 相關 open order
```

若 broker 有部位：

```text
LIVE_QUARANTINED
Reason = BROKER_POSITION_NOT_FLAT
```

### 10.3 禁止自動平倉

系統不得因切換失敗而自動送出真實平倉單。原因：

- 無法確定既有部位屬於哪個策略
- 無法確定部位是否為人工交易或 hedge
- 自動平倉本身可能造成新風險

---

## 11. State Namespace Isolation

### 11.1 目錄分離

```text
exports/trades/paper/    ← paper runtime state
exports/trades/live/     ← live runtime state
```

Paper 與 Live 不得共用 state path。

### 11.2 State Metadata

每份 state file 必須包含：

```json
{
  "execution_mode": "paper",
  "account_id_hash": null,
  "session_id": "PAPER-20260720-...",
  "process_start_id": "...",
  "schema_version": "1.0"
}
```

### 11.3 Mode Mismatch Guard

Live mode 讀到 Paper state 時 — 拒絕載入。

```python
if state.execution_mode != requested_mode:
    raise ModeStateMismatchError(...)
```

---

## 12. Position Authority Separation

Live mode 不使用 PaperTrader 作為 position authority。

```python
class PositionProvider:
    def get_positions(self): ...
    def get_open_orders(self): ...

class PaperPositionProvider(PositionProvider): ...
class BrokerPositionProvider(PositionProvider): ...
```

Mode binding：

```text
Paper mode → PaperPositionProvider
Live mode  → BrokerPositionProvider
```

---

## 13. PM2 Restart Behavior

每次 process start 都必須重新執行 broker reconciliation。

```text
requested_mode=live
→ effective_mode=live_preflight
→ load live namespace only
→ connect broker
→ broker position/order snapshots
→ reconciliation
   → pass: live_ready
   → fail: live_quarantined
```

不得沿用記憶體中的 `live_order_allowed=True`、reconciliation passed flag 或 lifecycle readiness。

---

## 14. Transition Audit

每次 mode transition 建立一份 append-only audit record。

```text
exports/trades/mode_transitions/
└── MODE-20260720-000001.json
```

內容：

```json
{
  "transition_id": "MODE-20260720-000001",
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

## 15. Failure Reason Codes

### Paper drain

```text
PAPER_POSITION_NOT_FLAT
PAPER_LIFECYCLE_NOT_FLAT
PAPER_PENDING_ORDERS_EXIST
PAPER_CALLBACKS_INFLIGHT
PAPER_ACTIVE_TRADE_EXISTS
PAPER_LEDGER_UNRESOLVED
```

### Broker preflight

```text
BROKER_NOT_CONNECTED
BROKER_AUTH_FAILED
BROKER_ACCOUNT_MISMATCH
BROKER_POSITION_NOT_FLAT
BROKER_OPEN_ORDERS_EXIST
BROKER_SNAPSHOT_STALE
```

### State isolation

```text
LIVE_STATE_MODE_MISMATCH
LIVE_STATE_NOT_CLEAN
LIVE_LEDGER_HAS_ACTIVE_TRADE
LIVE_LIFECYCLE_NOT_FLAT
LIVE_NAMESPACE_INVALID
```

### General

```text
RECONCILIATION_FAILED
TRANSITION_TIMEOUT
TRANSITION_INTERNAL_ERROR
```

---

## 16. Definition of Done (P0-A～P0-D)

首批 P0-A～P0-D 完成後，必須證明下列 10 項：

```text
1. config 改為 live 不會直接送真單
2. Paper 未 drain 時一定 block
3. Broker 有部位時一定 block
4. Broker 有 open order 時一定 block
5. Broker API 無法確認狀態時一定 block
6. PREPARED 未 COMMITTED 時一定 quarantine
7. PM2 restart 後一定重新 reconciliation
8. OrderManager 最終 gate 無法被策略層繞過
9. 舊 process callback 無法修改新 process context
10. live_order_allowed 只能由成功 transition service 設定
```

## 17. Phase 1+2+P0-D 完成後的能力邊界

完成後可以合理宣稱：

```text
✅ requested_mode=live 不會直接啟用真實下單
✅ Paper 未完全排空時，切換必定被阻擋
✅ Broker 有持倉或 open order 時無法切換
✅ 所有 broker order path 有 central hard gate
✅ PM2 重啟後重新 reconciliation
```

但還不能宣稱：

```text
❌ 系統已具備完整安全實盤能力
❌ Live/Paper state 已完全隔離（Phase 3 未完成）
❌ 可安全應對 broker 斷線後 reconnect（Phase 6 未完成）
```

## 17. Final Safety Invariant

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

---

## Appendix A: PR Dependency & Merge Gates

```text
PR1 — 建立不可繞過的安全底座 (Mode Model + Hard Gate)
  ↓
PR2 — 證明 Paper execution 已終止 (Paper Drain FSM)
  ↓
PR3 — 證明 Broker execution 可安全啟動 (Minimal Broker Preflight)
  ↓
PR4 — 將驗證結果安全提交並處理 crash/restart (Atomic Commit)
```

### PR 1: Mode Model + OrderManager Hard Gate

**完成後可宣稱：**

> `requested_mode=live` 不會直接授權任何 broker order；只有完整 transition 完成後才可能送真單。

**初始狀態（PR 1 合併後）：**

```python
requested_mode = LIVE
effective_mode = LIVE_PREFLIGHT
live_order_allowed = False
```

PR 2、PR 3 或 PR 4 尚未完成時，系統仍不能進入 `LIVE_READY`。

**Merge Gate：** 所有 live broker order path 在缺少有效 context 時皆被拒絕。

### PR 2: Paper Drain FSM

**Drain completion（全部滿足）：**

```text
position = 0
lifecycle = FLAT
pending orders = 0
inflight callbacks = 0
active trade_id = none
unresolved fills = 0
```

**Merge Gate：** 任一 Paper 非 terminal 狀態都無法進入 broker preflight。

### PR 3: Minimal Broker Preflight

**僅支援 Clean-Start-Only：**

```text
broker connected
account verified
positions empty
open orders empty
snapshots fresh
```

**Merge Gate：** Broker query 失敗、逾時、資料不完整均視為 failure，不得解讀為空倉。

### PR 4: Atomic Commit Checkpoint + Crash Recovery

**提交順序：**

```text
broker snapshot → reconciliation report → live state
→ manifest PREPARED → checkpoint COMMITTED → memory LIVE_READY
```

**Merge Gate：** 只有 COMMITTED checkpoint 與當前 process-local context 同時有效時，才能 activate。

### 第一版完成後可宣稱的能力

```text
✅ 系統僅支援 Clean-Start-Only 的 Paper → Live 切換
✅ 所有 Paper 狀態、Broker 狀態、持久化提交與
   當前 process authorization 均通過驗證後，才允許真實委託
```

**不能宣稱：**

```text
❌ 可安全接管既有實盤部位
❌ 可從任意 lifecycle 恢復
❌ 可自動處理人工單或未知舊倉
❌ 可在 broker 狀態不明時降級繼續運作
```

## Appendix B: 測試清單

### Unit Tests

- requested live + effective preflight → order blocked
- requested live + quarantined → order blocked
- requested live + ready + reconcile passed → order allowed
- paper mode → paper order path unchanged
- paper position nonzero → drain blocked
- paper lifecycle ARMED → drain blocked
- broker position exists → quarantined
- broker open order exists → quarantined
- broker disconnected → blocked
- account mismatch → blocked

### Fault Injection Tests

- Broker disconnect during preflight
- Process crash after paper drain but before broker reconciliation
- Process crash after broker reconciliation but before LIVE_READY commit
- Late paper callback arrives after live session initialized
