# Event Taxonomy v1 (事件分類學契約)

---

## 1. Overview (分類原則)
為防止在反事實重播中發生時序倒流、狀態偽造、或因果污染（例如模擬引擎錯誤地讀取了歷史已發生但在此模擬分支中不應存在的成交回報），我們對所有重播事件進行嚴格的權威度（Authority）與突變性（Mutability）分類。

每一類事件皆有固定的四元組標籤：
* **Origin** (來源層級): `OBSERVED` | `DERIVED` | `RECONSTRUCTED` | `COUNTERFACTUAL`
* **Causality** (因果屬性): `EXOGENOUS` (外生市場事件) | `ENDOGENOUS` (內生策略反饋)
* **Authority** (授權機構): `EXCHANGE` (交易所) | `BROKER` (券商) | `PRODUCTION_ENGINE` (實戰引擎) | `REPLAY_ENGINE` (重播引擎)
* **Mutability** (可變性判定): `IMMUTABLE` (不可變事實) | `REPLACEABLE` (可被覆寫/模擬取代)

---

## 2. Event Class Registry (事件大類註冊表)

### A. 市場外生事實 (Exogenous Market Facts)
這類事件為客觀市場狀態，不受策略行為影響。在反事實分支中**必須 100% 完整保留且不變**。

#### 1. `MARKET_TICK` (交易所行情)
* **Origin**: `OBSERVED`
* **Causality**: `EXOGENOUS`
* **Authority**: `EXCHANGE`
* **Mutability**: `IMMUTABLE`
* **Description**: 包含商品 Bid/Ask 價格與量、Last 成交價與量。

#### 2. `SESSION_BOUNDARY` (時段轉移)
* **Origin**: `OBSERVED`
* **Causality**: `EXOGENOUS`
* **Authority**: `EXCHANGE`
* **Mutability**: `IMMUTABLE`
* **Description**: 代表開盤、收盤、日盤夜盤切換。

---

### B. 券商回報事實 (Broker Response Facts)
這類事件是由原始策略行為引起的外部因果反饋。

#### 1. `BROKER_ACK` (委託回報確認)
* **Origin**: `OBSERVED`
* **Causality**: `ENDOGENOUS`
* **Authority**: `BROKER`
* **Mutability**: `REPLACEABLE`
* **Description**: 券商收到並接受委託單（單號、價格、數量）。

#### 2. `BROKER_FILL` (成交回報)
* **Origin**: `OBSERVED`
* **Causality**: `ENDOGENOUS`
* **Authority**: `BROKER`
* **Mutability**: `REPLACEABLE`
* **Description**: 券商通知成交資訊（成交價、量、手續費、稅金）。

---

### C. 生產衍生狀態 (Derived Production States)
原始實戰引擎中根據當時事實衍生計算出的狀態與日誌。

#### 1. `LIFECYCLE_TRANSITION` (生命週期轉移)
* **Origin**: `DERIVED`
* **Causality**: `ENDOGENOUS`
* **Authority**: `PRODUCTION_ENGINE`
* **Mutability**: `REPLACEABLE`
* **Description**: 實戰引擎狀態機（FSM）的狀態切換。

#### 2. `POSITION_STATE` (持倉快照)
* **Origin**: `DERIVED`
* **Causality**: `ENDOGENOUS`
* **Authority**: `PRODUCTION_ENGINE`
* **Mutability**: `REPLACEABLE`
* **Description**: 當時的真實持倉部位、未實現損益、保證金狀態。

---

### D. 系統基礎建設事件 (Infrastructure Events)
記錄生產運作時的斷線、故障與重啟事件。

#### 1. `PROCESS_RESTART` (進程重啟)
* **Origin**: `OBSERVED`
* **Causality**: `EXOGENOUS`
* **Authority**: `PRODUCTION_ENGINE`
* **Mutability**: `IMMUTABLE`
* **Description**: PM2 或主進程發生重啟的時點。

#### 2. `BROKER_DISCONNECT` / `BROKER_RECONNECT` (斷線與重連)
* **Origin**: `OBSERVED`
* **Causality**: `EXOGENOUS`
* **Authority**: `PRODUCTION_ENGINE`
* **Mutability**: `IMMUTABLE`
* **Description**: 與 Shioaji 券商 API 連線中斷或重連的時點。

#### 3. `STATE_RECONCILED` (部位核對對齊)
* **Origin**: `DERIVED`
* **Causality**: `EXOGENOUS`
* **Authority**: `PRODUCTION_ENGINE`
* **Mutability**: `IMMUTABLE`
* **Description**: 斷線重連後，實戰系統與券商 API 進行真實持倉對齊的事件。

---

### E. 反事實重播生成事件 (Counterfactual Virtual Events)
重播模擬時產生的虛擬事件。**在實戰數據集中不存在，僅在重播運作時由模擬器動態產生**。

#### 1. `VIRTUAL_ORDER_SUBMIT` (虛擬委託送出)
* **Origin**: `COUNTERFACTUAL`
* **Causality**: `ENDOGENOUS`
* **Authority**: `REPLAY_ENGINE`
* **Mutability**: `REPLACEABLE`
* **Description**: 反事實策略引擎生成的委託事件。

#### 2. `VIRTUAL_FILL` (虛擬模擬成交)
* **Origin**: `COUNTERFACTUAL`
* **Causality**: `ENDOGENOUS`
* **Authority**: `REPLAY_ENGINE`
* **Mutability**: `REPLACEABLE`
* **Description**: 由模擬執行模型（Execution Model）對 `VIRTUAL_ORDER_SUBMIT` 進行撮合後的模擬成交回報。

#### 3. `VIRTUAL_LIFECYCLE` (虛擬生命週期切換)
* **Origin**: `COUNTERFACTUAL`
* **Causality**: `ENDOGENOUS`
* **Authority**: `REPLAY_ENGINE`
* **Mutability**: `REPLACEABLE`
* **Description**: 虛擬策略引擎的狀態機轉移。
