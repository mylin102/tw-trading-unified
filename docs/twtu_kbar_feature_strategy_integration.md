# Kbar Feature Strategy Integration Notes

## 檔案
- `twtu_kbar_feature_strategy.py`

## 策略定位
這是一個以 **feature-enriched kbar** 為輸入的規則策略。
適合放在 `tw-trading-unified` 的 signal layer，而不是 broker adapter。

---

## 建議架構位置

```text
market data -> feature engine -> strategy -> risk manager -> order manager -> broker
```

此策略應該吃的是 **已經算好欄位的 dataframe / latest bar snapshot**：
- OHLCV: `open/high/low/close/volume`
- trend/filter: `regime`, `adx`, `bull_align`, `bear_align`, `bullish_align`, `bearish_align`
- momentum: `macd_hist`, `macd_rising`, `mom_velo`, `score`
- structure: `recent_high`, `recent_low`, `is_new_high`, `is_new_low`
- risk: `atr`, `vwap`, `price_vs_vwap`

---

## 最小接法

```python
import pandas as pd
from twtu_kbar_feature_strategy import KbarFeatureStrategy

feature_df = get_feature_dataframe(symbol="TXF")
strategy = KbarFeatureStrategy(
    long_enabled=False,
    short_enabled=True,
    adx_threshold=20,
)

latest_row = feature_df.iloc[-1]
entry_signal = strategy.generate_entry_signal(latest_row)

if entry_signal.action == "SELL":
    qty = strategy.calc_position_size(
        equity=account_equity,
        entry=float(latest_row["close"]),
        stop=float(entry_signal.stop_loss),
        size_mult=entry_signal.size_mult,
    )
    submit_short_order(qty=qty)
```

---

## 與 order manager 的分工

策略只負責：
- 是否產生 `BUY` / `SELL`
- 初始 `stop_loss`
- 初始 `take_profit`
- 建議的 `size_mult`

order manager 負責：
- 實際下單方式（市價 / 限價 / IOC / ROD）
- 委託是否成交
- partial fill 處理
- 成交均價更新
- 保護單送出
- cancel / replace
- replay / reconnect 後的 state recovery

---

## 持倉中出場判斷

```python
exit_signal = strategy.generate_exit_signal(
    row=latest_row,
    position_side="SHORT",
    entry_price=avg_entry_price,
    bars_held=bars_held,
    stop_loss=current_stop,
    take_profit=current_tp,
)

if exit_signal.action == "EXIT_SHORT":
    submit_cover_order(qty=current_position_qty)
```

---

## live trading 注意事項

### 1. 不要在未完成 bar 上直接反覆觸發
建議只在：
- bar close
- 或明確定義的 intrabar event

做一次 signal evaluation。

否則同一根 bar 可能重複送單。

### 2. strategy 不要直接持有 broker state
策略應只看：
- 最新 feature row
- 現有 position snapshot
- 已持有 bars 數

不要把成交狀態、委託狀態、broker reconnect 細節寫進 strategy class。

### 3. position snapshot 要由 order lifecycle 提供
至少要有：
- `side`
- `qty`
- `avg_price`
- `entry_time`
- `bars_held`
- `stop_loss`
- `take_profit`

### 4. 同商品單向唯一持倉
v1 建議同商品只允許：
- flat
- one long
- one short

先不要同時做 pyramiding 與 reverse-on-same-bar。

---

## 建議的 v1 實戰設定

### 台指 / 趨勢盤
- `long_enabled=False`
- `short_enabled=True`
- `adx_threshold=20`
- `require_breakout=True`
- `stop_atr_mult=1.2`
- `take_profit_atr_mult=2.0`
- `max_hold_bars=12`
- `risk_per_trade=0.005`

### 若訊號太少
依序調整：
1. `score_short_threshold`: `-20 -> -10`
2. `require_breakout`: `True -> False`
3. `adx_threshold`: `20 -> 18`

不要一開始就同時放寬全部。

---

## 建議下一步

1. 接進你的 feature pipeline
2. 每根 bar 輸出一份 `entry_reason / reject_reason`
3. 跑至少 1~3 個月歷史資料
4. 看：
   - 觸發頻率
   - 勝率
   - expectancy
   - 最大連虧
   - regime 分布
5. 再決定要不要開 long side

---

## 最重要的工程原則

**策略判斷** 和 **委託生命週期** 必須分離。

你前面遇到的很多實戰問題，通常不是 alpha 不夠，而是：
- signal 重複觸發
- 同一訊號重複送單
- fill state 不一致
- restart 後 position state 遺失
- strategy layer 直接碰 broker state

所以這個檔案的角色，應該只是一個乾淨的 decision engine。
