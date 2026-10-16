# Renko Governance Contract (MTS)

**Status**: ACTIVE (2026-08-02, Step 4A)
**Applies to**: Single-Leg Renko shadow, Spread Renko Shadow Collector (P2), any future Renko-based policy evaluation

---

## 核心原則 (Core Principle)

> **Renko 在 MTS 中只能是由原始 tick 衍生出的觀測事件，不得被視為可成交市場資料。**

Renko bricks are derived observation events. They are NOT market data that can be traded on.
No brick value (open/close/high/low) may ever be treated as a tradeable execution price.

---

## 治理契約 (10 條核心契約)

### 1. Brick close 不得作為 fill price
Renko brick close 不得直接作為 backtest、shadow 或 execution fill price。
任何以 brick close 計算的 PnL 都是 theoretical，不代表真實成交結果。

### 2. Signal 必須綁定 source tick
所有 Renko signal 必須綁定產生它的 source tick，至少包含：
- `source_receive_sequence`
- `source timestamp`
- `source input price`
- `bricks_created_this_tick`

缺任一欄位的 signal 不得進入 policy evaluation。

### 3. 同一 source tick 只執行一次 Policy Evaluation
同一 source tick 即使產生多個 bricks（multi-brick），也只能執行**一次** policy evaluation，且最多產生**一個** exit intent。禁止每 brick 觸發一次評估。

### 4. Counterfactual fill 使用 signal 可觀測後的第一筆 Executable Quote
Counterfactual fill 必須使用 signal 可觀測**之後**、經過 latency model 的**第一筆** executable quote：
- `LONG exit` 使用 **Bid**
- `SHORT exit` 使用 **Ask**

不得使用 signal 前的 quote 或 signal 同刻的 last trade。

### 5. 必須記錄並分開五種價格
必須分別記錄並明確區分：
1. `brick close price`
2. `source tick price`
3. `executable quote at signal`
4. `simulated fill` (counterfactual)
5. `actual paper fill` (若有)

### 6. 歷史研究從 tick/quote replay 重建
歷史研究必須從 tick／quote replay 重建 Renko，**不得**直接讀 Renko brick series 當市場價格做回測。Brick series 是事件日誌，不是價格源。

### 7. 分鐘 OHLC 重建標記為 APPROXIMATE
分鐘 OHLC 重建的 Renko 一律標記為 `APPROXIMATE`，不得用於 execution promotion。

### 8. Dashboard 必須顯示治理欄位
Dashboard 必須顯示：
- `Brick price is not fill price` 警示
- `same-tick brick batch count`
- `one policy evaluation per source tick`
- `signal-to-executable price gap`

### 9. Promotion Gate 三層 PnL 比較
Promotion gate（shadow → paper → execution）必須比較：
- `theoretical brick-close PnL`
- `executable shadow PnL`
- `actual paper-fill PnL`

### 10. Theoretical Edge 消失則不得升級 Execution
若 theoretical edge 在加入 Bid/Ask、latency、slippage 後消失，Renko execution **不得**升級（維持 shadow）。

---

## 處理順序 (Fixed Lifecycle Pipeline)

```
Session Calendar Gate
→ Quote Integrity
→ Jump Validation
→ Renko Brick Generation
→ Policy Evaluation (一次/tick)
→ Exit Intent (最多一個/tick)
```

不得先生成 brick 再拒絕異常 tick。

---

## 門檻語義 (Uniform Thresholds)

```
<30 pts      : ACCEPT
30–50 pts    : OBSERVE_ONLY (Step 4A — 正常處理 + telemetry；待 valid-session distribution 重算)
≥50 pts      : REJECT (0 bricks + 零 mutation)
```

---

## 相關元件 (System Architecture Mapping)

- `strategies/plugins/futures/active/renko_tracker.py` — Renko Tracker & Canonical Brick Event Generator
- `strategies/plugins/futures/active/tmf_spread.py` — Session Gate & MTS Lifecycle Integration
- `ui/renko_renderer.py` — Renko CLI & Web Dashboard Visualization Renderer
- `core/quote_integrity.py` — Executable Bid/Ask Quote Integrity Gate
