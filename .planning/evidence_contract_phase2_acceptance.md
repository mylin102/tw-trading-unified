# Phase 2 Evidence Contract — 委託真相層資料品質缺口：stale PendingSubmit 與 covered matching 正式驗收案例

**狀態**: PHASE-2 SPEC（2026-08-17 盤中觀察衍生；Hermes 記錄，Codex Phase 2 實作）
**前置**: Phase 1 Evidence Contract（core/broker_evidence.py，29 tests green，unconnected）
**範圍**: 僅 Evidence capture 去重 + covered matching 驗證；不接線 production runtime、不改策略/下單行為

---

## 1. 已知事實（2026-08-17 盤中實證）

1. `ORD-20260817-000008/000009`（TMFH6 MTS_ENTRY / TMFI6 MTS_ENTRY）各自已有本地 `LEG_FILLED`（13:41:08 / 13:41:17，trade `mts-auto-134033-293`），但 broker `list_trades` 仍回傳 `PendingSubmit`（broker id `4ad415e3` / `842914f0`）。
2. capture 將相同 order 重複列舉（nested `order` fallback + top-level 身份同時命中），造成 2 個唯一 order 被展開成 4 個 candidates（broker_trades 20 筆 = 10 筆唯一 × 2）。
3. `_position_covered_orders()` 因 `len(candidates)=4 != len(active_positions)=2` fail-closed，無法過濾 position-covered entry orders → open_orders 顯示 4 筆 stale `PendingSubmit`。

**判定**: 這不是「兩筆已確認成交 entry 的緊急問題」就能結案的。它是**委託真相層的資料品質缺口**；在 dedupe 與一對一 matching 通過測試前，不得把 covered filter 擴大成普遍規則，也不得宣稱 order lifecycle 已完整修好。

---

## 2. 修復要求

### 2.1 先在 Evidence capture 層依穩定身份去重

- 優先使用 broker order ID（`broker_order_id` / nested `order.id`）；**不得只靠 code／方向／qty** 去重。
- 同一 order 多次 capture 只能形成一筆 canonical evidence。
- 保留 duplicate counter 與原始觀測數（`raw_observations` / `duplicates_suppressed`），**不能靜默吞掉**。

### 2.2 定義證據優先序

- broker 明確 `Filled/Cancelled/Rejected` 才是 broker 終態。
- 本地 `LEG_FILLED` + broker position 只能判定該 entry order 已被 **position covered**，**不能竄改 broker 原始狀態為 `Filled`**。
- UI/輸出應區分：
  - `broker_status=PendingSubmit`（原始 broker 狀態，保留稽核）
  - `effective_status=POSITION_COVERED`（推導結果，非 broker 終態）

### 2.3 covered matching 必須是一對一

- order identity 去重後，對 code、方向、qty 做 **deterministic matching**。
- 任一數量不符、身份缺失或多義匹配 → 維持 active／unconfirmed，**不能猜測清除**。
- 不可讓真正的 pending exit、加碼單或反向單被現有持倉錯誤 covered。

### 2.4 不可依賴「整體 candidates 數量等於 positions 數量」作為唯一條件

- 應按商品與方向**逐組核對**（per-code / per-direction matching）。
- 例如 TMFH6 的重複不能影響 TMFI6 的 matching 結果。

### 2.5 restart 後結果必須一致

- 重啟後重新取得相同 broker snapshot，仍應得到：
  - 2 個 unique orders
  - 兩筆 `POSITION_COVERED`
  - effective `open_orders=[]`
- 原始 broker `PendingSubmit` evidence 必須保留供稽核。

---

## 3. 必測案例

| # | 案例 | 期望 |
|---|------|------|
| 1 | 2 unique filled entries × 每筆重複 capture 2 次 | 去重為 2，兩筆皆 covered |
| 2 | 相同 code／方向／qty，但 broker ID 不同的兩張真實委託 | 不得誤去重（2 筆都保留） |
| 3 | 一筆 covered entry + 一筆真正 pending exit | 只清 entry，exit 保留 |
| 4 | position qty 小於、等於、大於 pending qty | 小於 → 不 covered；等於 → covered；大於 → covered（qty 語意明確界定） |
| 5 | 缺 broker ID | 維持 active／unconfirmed，不猜測清除 |
| 6 | 重啟（相同 snapshot 重放） | 結果一致（2 unique / 2 covered / open_orders=[]） |
| 7 | capture 次序改變（identical rows 但不同排列） | 去重與 matching 結果不變（deterministic） |
| 8 | broker 後續轉成 `Filled` | canonical record 正常升級為終態，**不產生第二筆 order** |

---

## 4. 驗收輸出契約

每個 covered-matching 案例的輸出至少要顯示：

```
raw_observations=4        # 原始 capture 觀測數（重複展開前）
unique_orders=2           # 去重後唯一 order 數
duplicates_suppressed=2   # 被去重消除的重複數
position_covered=2        # 被 position 一對一 covered 的 order 數
effective_open_orders=0   # 過濾後 effective open orders
```

加上 per-order：

```
broker_order_id=4ad415e3  broker_status=PendingSubmit  effective_status=POSITION_COVERED
broker_order_id=842914f0  broker_status=PendingSubmit  effective_status=POSITION_COVERED
```

---

## 5. 邊界（不可越界）

- 不改 `_position_covered_orders()` 成為普遍規則（放寬數量條件）；covered filter 的修正只發生在 **去重後、一對一、per-code-per-direction** 的語意內。
- 不竄改 broker 原始 `PendingSubmit` 狀態為 `Filled`。
- 不動 strategy thresholds / submission behavior / PM2 / deployment / restart / replay / broker state。
- Phase 2 只交付契約層驗證（fake-API RED→GREEN）；production 接線另立 phase，經 codex review 後才進行。
