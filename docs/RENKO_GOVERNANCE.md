# Renko Governance Contract (MTS)

**Status**: ACTIVE (2026-08-02, Step 4A)
**Applies to**: Single-Leg Renko shadow, Spread Renko Shadow Collector (P2), any future Renko-based policy evaluation

---

## 核心原則

> **Renko 在 MTS 中只能是由原始 tick 衍生出的觀測事件，不得被視為可成交市場資料。**

Renko bricks are derived observations. They are NOT market data that can be traded on.
No brick value (open/close/high/low) may ever be treated as a tradeable price.

---

## 治理契約（10 條）

### 1. Brick close 不是 fill price
Renko brick close 不得直接作為 backtest、shadow 或 execution 的 fill price。
任何以 brick close 計算的 PnL 都是 theoretical，不是可成交結果。

### 2. Signal 必須綁定 source tick
所有 Renko signal 必須綁定產生它的 source tick，至少包含：
- `source_receive_sequence`
- `source timestamp`
- `source input price`
- `bricks_created_this_tick`

缺任一欄位的 signal 不得進入 policy evaluation。

### 3. 同一 source tick 一次 policy evaluation
同一 source tick 即使產生多個 bricks（multi-brick），也只能執行**一次** policy
evaluation，且最多產生**一個** exit intent。禁止每 brick 觸發一次評估。

### 4. Counterfactual fill 用 signal 後第一筆 executable quote
Counterfactual fill 必須使用 signal 可觀測**之後**、經過 latency model 的**第一筆**
executable quote：
- LONG exit 使用 Bid
- SHORT exit 使用 Ask

不得用 signal 前的 quote 或 signal 同刻的 last trade。

### 5. 五種價格分開記錄
必須分別記錄並可區分：
1. brick close price
2. source tick price
3. executable quote at signal
4. simulated fill（counterfactual）
5. actual paper fill（若有）

### 6. 歷史研究從 tick/quote replay 重建
歷史研究必須從 tick／quote replay 重建 Renko，**不得**直接讀 Renko brick series
當市場價格做回測。Brick series 是事件日誌，不是價格源。

### 7. 分鐘 OHLC 重建標記 APPROXIMATE
分鐘 OHLC 重建的 Renko 一律標記為 `APPROXIMATE`，不得用於 execution promotion。

### 8. Dashboard 顯示必要欄位
Dashboard 必須顯示：
- "Brick price is not fill price"
- same-tick brick batch count
- one policy evaluation per source tick
- signal-to-executable price gap

### 9. Promotion gate 三層 PnL 比較
Promotion gate（shadow → paper → execution）必須比較：
- theoretical brick-close PnL
- executable shadow PnL
- actual paper-fill PnL

### 10. Edge 消失不得升級
若 theoretical edge 在加入 Bid/Ask、latency、slippage 後消失，
Renko execution **不得**升級（維持 shadow）。

---

## 處理順序（固定）

```
Session Calendar Gate
→ Quote Integrity
→ Jump Validation
→ Renko Brick Generation
→ Policy Evaluation（一次/tick）
→ Exit Intent（最多一個/tick）
```

不得先生成 brick 再拒絕異常 tick。

---

## 門檻語義（統一）

```
<30 pts      : ACCEPT
30–50 pts    : OBSERVE_ONLY（Step 4A — 正常處理 + telemetry；待 valid-session
               distribution 重算後再決定是否改 QUARANTINE）
≥50 pts      : REJECT（0 bricks + 零 mutation）
```

> 50 是 REJECT threshold，不是 warning。80 僅為 outlier marker。
> （見 tests/fixtures/jump_policy_fixture.json — schema_version 1）

---

## 相關元件

- `strategies/plugins/futures/active/renko_tracker.py` — tracker（Step 4A: jump
  全狀態檢查、無 cap-drop、負數回傳待 API 遷移）
- `strategies/plugins/futures/active/tmf_spread.py` — Session Gate + Gap
  Re-entry（RENKO_SESSION_GATE_REJECT / RENKO_GAP_QUARANTINE）
- `core/date_utils.py::is_taifex_trading_session` — 正確 TAIFEX calendar
  （day 08:45-13:45 / night 15:00-05:00）
- `core/quote_integrity.py` — Quote Integrity Gate（P0b）
- `tests/fixtures/jump_policy_fixture.json` — 門檻與具名案例

## 已知待辦

- [ ] add() 負數回傳遷移（先盤點 callers — 測試暫以 abs() 比較）
- [ ] 30-50 QUARANTINE 決策（Session Gate 上線後重算 valid-session distribution）
- [ ] P2 SpreadRenkoShadowCollector（需 Spread Synchronizer 驗收後）
- [ ] Dashboard 治理欄位（契約 8）
