# ADR-017: Trajectory Evidence Model (軌跡證據模型)

## Status
Proposed (Draft)

## Context (背景)
在 Counterfactual Lab v1.0.0 中，我們成功實現了**點重播驗證 (Point Replay)**，即在孤立的決策時點（Decision Point）比對歷史決策與當前代碼。然而，點重播只處理了離散的靜態切片，無法回答：
1. 決策改變後，對後續持倉狀態（State Chronology）的累積影響。
2. 策略在完整事件軌跡（Event Trajectory）演進過程中的行為與轉移邏輯。

進入 Phase 4 (Trajectory Replay) 的首要挑戰在於**因果污染（Causality Contamination）**。如果在參數擾動後，系統仍舊錯誤地注入歷史已發生的成交回報（Fills）或部位狀態，反事實模擬將失去真實性。因此，我們必須對歷史事實、衍生狀態與模擬結果進行嚴格的權威分類。

## Decision (決策)
我們決定在設計 Trajectory Replay Runner 前，首先確立並發布 **Trajectory Evidence Model（軌跡證據模型）**。所有事件（Events）必須以統一的資料規格定址，並具備明確的權威與因果元數據（Metadata）。

### 1. 核心治理四元組 (Core Governance Metadata)
每個事件記錄中必須包含以下四個元數據欄位，用以規範其在反事實模擬中的保留或失效規則：

| 欄位名 (Field) | 允許值 (Values) | 定義與治理目的 |
| :--- | :--- | :--- |
| `origin` | `OBSERVED`<br>`DERIVED`<br>`RECONSTRUCTED`<br>`COUNTERFACTUAL` | 區分原始觀測值（實戰記錄）、由觀測值計算出的狀態、於 Replay 時重新組裝的狀態、以及反事實模擬生成的非歷史狀態。 |
| `authority` | `EXCHANGE`<br>`BROKER`<br>`PRODUCTION_ENGINE`<br>`REPLAY_ENGINE` | 標識事件的授權來源（交易所、經紀商、實戰引擎、或重播模擬器）。 |
| `causality` | `EXOGENOUS`<br>`ENDOGENOUS` | **外生（Exogenous）**：源自市場（如 Tick 價格、交易時段）；<br>**內生（Endogenous）**：源自策略行為的因果反饋（如委託、成交回報）。 |
| `mutability` | `IMMUTABLE`<br>`REPLACEABLE` | **不可變（Immutable）**：在反事實分支中不可被修改或刪除（如交易所行情）；<br>**可替代（Replaceable）**：當決策分歧後，必須被模擬事件取代的項目。 |

### 2. 軌跡事件最小 schema 規格
每一個事件對象必須符合以下最小欄位契約：
```json
{
  "event_id": "evt-uuid-v4",
  "event_type": "MARKET_TICK | BROKER_FILL | ...",
  "event_time": 1784268000000000000,
  "receive_time": 1784268000005000000,
  "source": "shioaji | exchange | production_fsm",
  "source_sequence": 123456,
  "trade_id": "t-20260717-001",
  "session_id": "s-20260717-day",
  "origin": "OBSERVED",
  "authority": "EXCHANGE",
  "causality": "EXOGENOUS",
  "mutability": "IMMUTABLE",
  "payload_schema_version": "v1.0.0",
  "quality_flags": 0
}
```

### 3. 事件分類學 (Event Taxonomy v1)
系統定義的事件大類及其元數據分類如下：

1. **外生交易所事件 (Observed Exogenous Facts)**
   * `MARKET_TICK` (Exchange Quote/Trade)
     * Origin: `OBSERVED`, Authority: `EXCHANGE`, Causality: `EXOGENOUS`, Mutability: `IMMUTABLE`
   * `SESSION_BOUNDARY` (Day/Night transition)
     * Origin: `OBSERVED`, Authority: `EXCHANGE`, Causality: `EXOGENOUS`, Mutability: `IMMUTABLE`
2. **原始內生成交與狀態 (Observed Endogenous Facts)**
   * `BROKER_ACK` / `BROKER_FILL` (Recorded Execution)
     * Origin: `OBSERVED`, Authority: `BROKER`, Causality: `ENDOGENOUS`, Mutability: `REPLACEABLE`
   * `SYSTEM_RESTART` / `BROKER_DISCONNECT` (Infrastructure events)
     * Origin: `OBSERVED`, Authority: `PRODUCTION_ENGINE`, Causality: `EXOGENOUS`, Mutability: `IMMUTABLE`
3. **衍生生產狀態 (Derived Production State)**
   * `LIFECYCLE_TRANSITION` (Production state machine transition)
     * Origin: `DERIVED`, Authority: `PRODUCTION_ENGINE`, Causality: `ENDOGENOUS`, Mutability: `REPLACEABLE`
   * `POSITION_STATE` (Current holding state before/after)
     * Origin: `DERIVED`, Authority: `PRODUCTION_ENGINE`, Causality: `ENDOGENOUS`, Mutability: `REPLACEABLE`
4. **反事實模擬事件 (Counterfactual Replay State)**
   * `VIRTUAL_FILL` (Simulated counterfactual execution)
     * Origin: `COUNTERFACTUAL`, Authority: `REPLAY_ENGINE`, Causality: `ENDOGENOUS`, Mutability: `REPLACEABLE`
   * `VIRTUAL_LIFECYCLE` (Simulated counterfactual state machine transition)
     * Origin: `COUNTERFACTUAL`, Authority: `REPLAY_ENGINE`, Causality: `ENDOGENOUS`, Mutability: `REPLACEABLE`

## Consequences (後果)
1. **防堵因果污染**：當反事實決策與歷史不同（例如未提交 release）時，Replay Engine 可藉由 `Causality == ENDOGENOUS` 與 `Mutability == REPLACEABLE` 的標記，自動將之後的歷史 `BROKER_FILL` 事件過濾失效，移交給 `ExecutionModel` 生成對應的 `VIRTUAL_FILL`，避免時間與狀態倒流的邏輯悖論。
2. **資料契約化**：Phase 4A 的軌跡資料集編譯程序必須嚴格產出符合本模型的 Parquet 格式事件日誌，並包含此四元組元數據，否則拒絕重播。
3. **與後續 ADR 關係**：ADR-018 (Deterministic Ordering) 將基於本模型定義 Ordering Key；ADR-020 (Historical Truth Boundary) 將基於此元數據判定分叉點並進行動態事件過濾。
