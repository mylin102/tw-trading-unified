# tw-trading-unified 風格策略模組建議

## 建議檔案位置

```text
src/
  strategies/
    kbar_feature_strategy.py
  models/
    signals.py
    positions.py
  services/
    feature_service.py
    position_service.py
    order_service.py
  runners/
    live_signal_loop.py
```

---

## 建議責任分界

### strategy module
只做：
- 讀取 latest feature row
- 根據 flat / long / short 狀態輸出 decision
- 產生 stop loss / take profit / size multiplier

不要做：
- 直接下單
- 查 broker 委託狀態
- 查成交回報
- 自己維護 fill state

### position service
提供：
- 現有持倉 side
- qty
- avg_price
- bars_held
- 目前 stop / tp

### order service
負責：
- submit / cancel / replace
- partial fill
- avg fill update
- restart recovery
- reconciliation

---

## main loop 範例流程

```python
feature_df = feature_service.get_feature_df(symbol)
latest = feature_df.iloc[-1]
position = position_service.get_position_snapshot(symbol)

decision = strategy.evaluate(latest, position)

if decision.action in ["BUY", "SELL"]:
    risk_checked = risk_service.allow_new_entry(symbol, decision)
    if risk_checked:
        order_service.submit_entry(symbol, decision)

elif decision.action in ["EXIT_LONG", "EXIT_SHORT"]:
    order_service.submit_exit(symbol, decision)
```

---

## 跟你現有系統最有關的幾點

### 1. signal 不要直接碰委託狀態
因為你之前遇到的核心問題，很像是：
- 同一根 bar 重複判斷
- 多個 monitor 同時送單
- process restart 後 state 不一致

所以這版故意讓 strategy 完全不知道 broker 端到底有沒有 pending order。

這件事要在 order lifecycle 層擋：
- same symbol pending entry exists -> block new entry
- same bar same signal hash already sent -> dedupe
- position not flat but strategy still sees flat -> reconcile before trading

### 2. 只在 bar close 評估一次
v1 強烈建議：
- 每個商品
- 每根 bar
- 只允許一次 strategy evaluation

並記錄：
- `symbol`
- `bar timestamp`
- `strategy_name`
- `decision.action`
- `decision.reason`
- `signal_hash`

### 3. 先不要做同 bar 反手
避免這種情況：
- 先 EXIT_SHORT
- 同根 bar 又 BUY

v1 建議：
- exit bar != new entry bar
- 或至少設 cooldown 1 bar

### 4. 最先加的不是更多 alpha，而是 reject reason log
建議每次沒下單都留：
- regime rejected
- adx too low
- not below vwap
- score not strong enough
- breakout not confirmed
- position already open
- pending order exists

這會讓你很快知道：
- 為什麼沒訊號
- 是 feature 問題還是規則太嚴
- 是策略問題還是 order lifecycle 問題

---

## v1 建議設定

### 期貨 / 趨勢延續
```python
KbarFeatureStrategy(
    symbol="TXF",
    long_enabled=False,
    short_enabled=True,
    adx_threshold=20,
    require_breakout=True,
    stop_atr_mult=1.2,
    take_profit_atr_mult=2.0,
    max_hold_bars=12,
    risk_per_trade=0.005,
)
```

### 若訊號太少，調整順序
1. `score_short_threshold: -20 -> -10`
2. `require_breakout: True -> False`
3. `adx_threshold: 20 -> 18`

不要一次全部放寬。

---

## 建議你下一步接法

1. 先把 `twtu_repo_style_kbar_strategy.py` 放進 repo 的 strategy 目錄
2. 用一個 symbol 先 dry run
3. 每 bar 輸出 decision log
4. 確認沒有重複送單
5. 再接真的 order service
6. 最後才開多商品

這樣最能避免又回到「策略以為沒單，但 broker 其實已有單」的老問題。
