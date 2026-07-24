# MTS Z-score Warm Start 設計說明

## 目標

解決開盤初期因本 session 累積 bar / tick 不足，導致 `spread_z`、`spread_ma`、`spread_std`、`spread_ema_20`、`spread_ema_60` 為空或代表性不足的問題。

核心設計：

```text
不要沿用上一個 session 的 Z-score。
要沿用上一個 session 的統計狀態。
```

也就是開盤時以昨日或上一個有效 session 最後一筆統計量作為 warm start，讓第一筆即時價差即可計算 Z-score 與 EMA 趨勢。

---

## 現況問題

目前設計在新 session 開始時，rolling 指標需要重新累積：

```text
spread_ma      = rolling_mean(spread, 20)
spread_std     = rolling_std(spread, 20)
spread_ema_20  = EMA(spread, span=20)
spread_ema_60  = EMA(spread, span=60)
```

因此開盤初期可能出現：

```text
bar 數 < 20 → spread_ma / spread_std / spread_z 不可用
bar 數 < 60 → EMA60 或長窗口趨勢代表性不足
```

這會造成：

1. 開盤高波動區間缺少有效 Z-score。
2. Dashboard 前段指標空白或失真。
3. ENTRY gate 無法判斷偏離程度。
4. 若強制補零，可能導致錯誤訊號或誤判。

---

## 不建議做法：直接沿用上一個 session 的 Z-score

不要這樣做：

```python
current_spread_z = previous_session_last_spread_z
```

原因：Z-score 是由「當下 spread」相對於統計基準計算而來。上一個 session 的 Z-score 只能描述上一筆 spread 的偏離，不能描述今天開盤後的新 spread。

錯誤示例：

```text
昨日最後：
spread = -280
ma     = -260
std    = 10
z      = -2.0

今日開盤：
spread = -320
```

若直接沿用昨日 `z = -2.0`，會低估今日開盤偏離。

正確做法應該是：

```text
z = (current_spread - previous_ma) / previous_std
```

---

## 建議做法：沿用上一個 session 的統計狀態

開盤時載入上一個有效 session 最後一筆：

```text
spread_ma
spread_std
spread_ema_20
spread_ema_60
```

收到第一筆 tick 後：

```text
current_spread = near_tick_price - far_tick_price
current_z      = (current_spread - previous_spread_ma) / previous_spread_std
```

若未來改成 `far - near`，公式方向需全系統一致翻轉；本設計不依賴 spread 正負，只依賴一致性。

---

## 建議資料欄位

CSV 或 cache 至少應保留以下欄位：

```text
timestamp
near_close
far_close
spread
spread_ma
spread_std
spread_z
spread_ema_20
spread_ema_60
```

若要支援 warm start，建議新增 metadata 或狀態檔：

```json
{
  "last_valid_ts": "2026-07-09T13:44:00+08:00",
  "spread_ma": -260.5,
  "spread_std": 12.3,
  "spread_ema_20": -262.1,
  "spread_ema_60": -258.7,
  "source": "tmf_calendar_spread_20260709.csv"
}
```

可存於：

```text
/tmp/mts_spread_indicator_state.json
```

或由最新 CSV 最後一筆有效 row 直接恢復。

---

## 線上計算流程

### 開盤初始化

```text
1. 讀取今日 CSV。
2. 若今日 CSV 尚無足夠資料，讀取上一個有效 session 最後一筆指標狀態。
3. 初始化：
   - spread_ma
   - spread_std
   - spread_ema_20
   - spread_ema_60
4. 標記 indicator_warm_started = True。
```

---

### Tick 到來時計算

```python
rt_spread = near_tick_price - far_tick_price

if spread_std and spread_std > min_spread_std:
    spread_z = (rt_spread - spread_ma) / spread_std
else:
    spread_z = None
```

EMA 可用 recursive update：

```python
alpha_20 = 2 / (20 + 1)
alpha_60 = 2 / (60 + 1)

spread_ema_20 = alpha_20 * rt_spread + (1 - alpha_20) * prev_spread_ema_20
spread_ema_60 = alpha_60 * rt_spread + (1 - alpha_60) * prev_spread_ema_60
```

---

## Rolling MA / STD 的更新策略

建議採用跨 session rolling，不要每天重置：

```text
昨日最後 N-1 根 bar + 今日第一根 bar = 今日第一筆 rolling 統計
```

優點：

1. 開盤立即有有效統計量。
2. 避免前 20 根 bar 統計空窗。
3. Calendar spread 具連續性，跨 session 合理。
4. Z-score 不會因 session 切換突然失效。

---

## Fallback 規則

### 可接受 fallback

```text
使用上一個有效 session 的 spread_ma / spread_std / EMA 狀態。
```

### 不可接受 fallback

```text
spread_ma = 0
spread_std = 0
spread_z = 0
```

原因：補零會讓策略誤以為價差沒有偏離，或導致除零、錯誤 gate。

---

## 安全條件

### 1. 最小標準差保護

避免 `spread_std` 過小導致 Z-score 被放大：

```python
MIN_SPREAD_STD = 1.0  # 可依實際點數調整

if spread_std < MIN_SPREAD_STD:
    spread_z = None
    block_entry_reason = "SPREAD_STD_TOO_SMALL"
```

---

### 2. Warm start 時效限制

若上一個有效狀態過舊，不應使用：

```text
若 last_valid_ts 距今超過 max_warm_start_age，則不啟用 warm start。
```

建議：

```text
日盤開盤：允許使用前一夜盤或前一交易日收盤狀態
夜盤開盤：允許使用當日日盤收盤狀態
跨週末：可使用，但應加上 stale 標記或要求更多 confirm
長假後：不建議直接信任
```

---

### 3. 合約換月保護

若 near / far 合約代碼與上一個狀態不同，不能直接 warm start，除非確認是同一組 calendar pair。

需檢查：

```text
previous_near_symbol == current_near_symbol
previous_far_symbol  == current_far_symbol
```

若不同：

```text
indicator_warm_started = False
block_entry_reason = "CONTRACT_PAIR_CHANGED"
```

---

## Dashboard 呈現建議

Row 2：

```text
Spread
Spread EMA20
Spread EMA60
±1 Std Dev band
```

Row 3：

```text
Raw Z-score
Entry threshold: ±3.0
Exit threshold: ±0.5
Stop threshold: ±3.5
Zero line
```

若資料來自 warm start，建議在圖上或 sidebar 顯示：

```text
Indicator mode: WARM_START
Last baseline: 2026-07-09 13:44:00
```

若已累積足夠本 session bars：

```text
Indicator mode: LIVE_ROLLING
```

---

## 策略使用建議

Warm start 只解決「指標可用性」。策略邏輯仍建議維持：

```text
ENTRY = Raw Z-score 極端偏離 + EMA20 slope / EMA20 vs EMA60 趨勢確認
RELEASE = PnL / release stop / trail / emergency
STOP = PnL-based stop
```

不要讓 EMA 取代 PnL release / stop。

---

## 建議實作步驟

### Step 1：資料端

在 `fetch_calendar_spread_data.py` / `update_calendar_spread.py`：

1. 計算並輸出：
   - `spread_ema_20`
   - `spread_ema_60`
2. 保留最後一筆有效狀態。
3. 禁止以 0 補 `spread_ma` / `spread_std` / `spread_z`。

---

### Step 2：Loader 端

在 `spread_loader.py`：

1. 載入最新 CSV。
2. 若今日資料不足，讀取上一個有效 session 最後一筆。
3. 回傳欄位：
   - `spread_ma`
   - `spread_std`
   - `spread_ema_20`
   - `spread_ema_60`
   - `indicator_mode`
   - `baseline_ts`

---

### Step 3：Monitor 即時計算端

在 `monitor.py`：

1. 使用即時 tick 計算 `rt_spread`。
2. 使用 warm-started `spread_ma` / `spread_std` 計算即時 `spread_z`。
3. 使用 recursive EMA 更新 `spread_ema_20` / `spread_ema_60`。
4. 若 `spread_std <= MIN_SPREAD_STD`，block entry。

---

### Step 4：Dashboard 端

在 `dashboard.py`：

1. Row 2 顯示 `Spread + EMA20 + EMA60`。
2. Row 3 顯示 raw `spread_z`。
3. 顯示 `indicator_mode` 與 `baseline_ts`。

---

## 驗收條件

### 功能驗收

1. 開盤第一筆有效 near/far tick 後，即可計算 `spread_z`。
2. 不再出現前 20 根 bar 無 Z-score 的空窗。
3. `spread_std = 0` 或過小時，不會產生異常 Z-score。
4. 合約 pair 改變時，不會誤用舊狀態。
5. Dashboard 可看出目前是 `WARM_START` 或 `LIVE_ROLLING`。

### 測試案例

建議新增：

```text
test_warm_start_uses_previous_ma_std
test_warm_start_does_not_reuse_previous_zscore
test_warm_start_blocks_when_std_too_small
test_warm_start_blocks_when_contract_pair_changed
test_indicator_mode_switches_to_live_rolling_after_enough_bars
```

---

## 總結

建議採用：

```text
跨 session 統計狀態 warm start
```

而不是：

```text
直接沿用上一個 session 的 spread_z
```

這樣可以解決開盤指標空窗，同時保持 Z-score 的數學正確性。對 MTS calendar spread 來說，價差本身具連續性，因此跨 session rolling / warm start 是合理設計。
