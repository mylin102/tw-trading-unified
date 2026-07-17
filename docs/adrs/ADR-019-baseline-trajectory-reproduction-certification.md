# ADR-019: Baseline Trajectory Reproduction Certification (基線軌跡重現性認證)

## Status
Proposed (Draft)

## Context (背景)
在反事實研究中，我們希望評估「當策略參數改變時，持倉與收益的變化」。但在進行任何反事實干預（Counterfactual Run）之前，我們必須建立對重播引擎（Replay Engine）的**還原信任度**。

如果 Replay Engine 甚至無法在**原始參數**下 100% 還原歷史交易軌跡，那麼反事實模擬得出的任何結論都將毫無價值。因此，我們必須對「基線重播重現性」建立正式的認證（Certification）流程與判定關卡（Gates）。這不只是一個總匹配率，而是涵蓋時間、價格、決策理由與狀態機變更的多維校驗。

## Decision (決策)
我們決定在 Trajectory Replay 模組中，將 **Baseline Trajectory Reproduction Certification** 確立為運行反事實掃描前的強制准入關卡。系統將對重播軌跡與歷史實戰軌跡在三個層次進行多維比對：

### 1. 嚴格語意比對層 (Exact Semantic Match)
此層次的屬性在基線重播中**必須 100% 完全一致**，不允許任何偏差：
* **Lifecycle Transition Sequence**：狀態機（FSM）的切換順序（如 `IDLE` ➔ `ARMED` ➔ `ACTIVE` ➔ `RELEASE_CONFIRMED` ➔ `IDLE`）。
* **Order Intent Type & Side & Leg**：發出的委託意圖（如 `BUY_SPREAD`, `SELL_SPREAD`）與委託方向、單/雙腳。
* **Position State Sequence**：部位狀態序列（包含部位數量的變化與清空時點）。
* **FSM Exit Reason**：狀態機因何種理由退出或轉移的 Reason 字串。

### 2. 誤差容忍比對層 (Tolerance-Based Match)
由於實戰環境中存在網路延遲、非確定性 callback 順序等微觀因素，以下物理度量允許在設定的容忍區間（Tolerances）內通過：
* **Transition & Callback Time (時間容差)**：虛擬狀態轉移時間與歷史記錄時間的差值 $|\Delta t| \le \text{Threshold}$ (預設為 500ms)。
* **Fill Price (價格容差)**：虛擬成交價與歷史實戰成交價的差值 $|\Delta P| \le \text{Threshold}$ (預設為 0 pt，即基線重播下必須精確對齊歷史 Fill)。
* **Quote Age (報價時效)**：撮合報價的延遲時間在合理區間內。

### 3. 僅供資訊記錄層 (Informational-Only)
以下欄位在比對中被忽略，不影響認證判定：
* 重播運行時產生的 UUID、日誌時間戳、實體執行緒 ID 等系統級隨機變量。

### 4. 認證輸出契約 (Certification Report Schema)
認證組件運作後，必須輸出如下結構的 JSON 認證報告：
```json
{
  "reproduction_status": "PASSED | FAILED",
  "metrics": {
    "transition_sequence_match": 1.0,
    "order_intent_match": 1.0,
    "position_state_match": 1.0,
    "reason_match": 1.0,
    "timing_tolerance_pass_rate": 0.98,
    "price_tolerance_pass_rate": 1.0
  },
  "first_divergence_event": null
}
```
* **First Divergence Event**：若認證失敗，此欄位必須記錄**第一個發生分歧的事件 ID、時間戳與分歧細節**（例如：歷史狀態為 `ACTIVE`，虛擬狀態轉移為 `ARMED`），以便研究人員快速定位引擎或數據集對齊問題。

## Consequences (後果)
1. **防堵不實結論**：若 `reproduction_status` 為 `FAILED`，系統將鎖死反事實敏感度掃描，不允許匯出任何實驗結果，確保所有匯出的敏感度報告都建立在可完全還原的基線引擎之上。
2. **定位精確化**：通過 First Divergence 追蹤，開發者能立即查出是由於「同 timestamp 排序規則（ADR-018）失效」或是「歷史 endogenous 事件注入錯誤」所造成的基線不一致。
