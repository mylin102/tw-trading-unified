# Phase 0 — TMF Calendar Spread Reduction Strategy

## 目標

建立一個：

# 極簡、可交易、低參數

的第一階段策略。

核心目的：

* 恢復交易流量
* 驗證 spread 結構是否有 edge
* 驗證 release → trailing 概念是否成立

---

# 核心概念

本策略不是：

* 傳統套利
* 純方向交易

而是：

# 「用遠近月 spread 偵測 breakout」

---

# 基本架構

## 初始狀態

進場時同時建立：

* Long 近月微台（TMF Near）
* Short 遠月微台（TMF Far）

例如：

```text
+1 TMF 近月
-1 TMF 遠月
```

---

# 為什麼預設：

## 多近月、空遠月？

因為：

正常情況下：

* 近月 beta 較高
* 波動較快
* breakout 時反應較強

因此：

```text
Long Near / Short Far
```

比較適合作為：

# breakout release 結構。

---

# 未來可研究方向（暫不加入）

未來可研究：

* spread momentum direction
* basis slope
* contango/backwardation
* intraday order flow

來決定：

```text
Long Near / Short Far
or
Short Near / Long Far
```

但：

# Phase 0 禁止增加方向判斷。

先固定：

```text
Long Near / Short Far
```

避免 complexity explosion。

---

# Entry 條件

## 僅在：

# SQUEEZE ON

時允許進場。

---

# SQUEEZE 定義（簡化版）

使用既有：

* BB Width
* ATR contraction
* ADX low

但：

# 不做 multi-regime routing。

只作為：

```text
市場進入低波動整理期
```

的單一條件。

---

# Entry 流程

## 當：

* squeeze_on == True
* 無持倉
* market_open == True

則：

建立：

```text
Long Near
Short Far
```

---

# Position Size

Phase 0 固定：

```text
1 : 1
```

即：

```text
+1 Near
-1 Far
```

---

# Stop Loss（Release Trigger）

## 任一腿：

浮動虧損超過：

# 20 點

則：

* stop 該腿
* 保留另一腿

---

# 範例

初始：

```text
+1 Near @ 22000
-1 Far  @ 21970
```

---

若：

Near 下跌：

```text
Near = 21980
```

則：

```text
Long Near = -20
```

觸發：

# Release

系統：

* 平掉 Near
* 保留 Short Far

變成：

```text
-1 Far naked short
```

---

# Release 後邏輯

當只剩單腿後：

進入：

# Trailing Mode

---

# Trailing Stop

固定：

# 20 點移動停利

---

# 做多情況

記錄：

```text
highest_price_since_release
```

若：

```text
highest - current >= 20
```

則：

# 停利出場。

---

# 做空情況

記錄：

```text
lowest_price_since_release
```

若：

```text
current - lowest >= 20
```

則：

# 停利出場。

---

# 數學定義

## Long trailing

highest_since_release - current \ge 20

---

## Short trailing

current - lowest_since_release \ge 20

---

# Re-entry

完全平倉後：

若：

```text
squeeze_on == True
```

可再次進場。

---

# 禁止項目（非常重要）

Phase 0：

# 禁止：

---

## Regime Routing

禁止：

* WEAK
* TREND
* BEAR
* TRANSITION

---

## Score System

禁止：

* ENTRY_SCORE
* strategy ranking
* bias sorting

---

## Cross Market

禁止：

* ETF resonance
* TX/TMF alignment
* bond confirmation

---

## Dynamic Hedge Ratio

禁止：

* +1/-0.7
* beta adjustment
* adaptive release

---

## Spread Analytics

禁止：

* z-score
* cointegration
* spread regression
* volatility surface

---

# 保留項目

## Risk

保留：

* max daily loss
* max concurrent position
* hard kill switch

---

## Observability

保留：

* ENTRY
* RELEASE
* TRAILING
* EXIT

完整 trace。

---

# 必須記錄的 telemetry

## 每筆交易：

```text
entry_ts
release_ts
release_side
release_reason
max_favorable_excursion
max_adverse_excursion
trailing_exit_price
holding_time
```

---

# 核心驗證問題

本 Phase 的真正目的：

不是獲利最大化。

而是回答：

# 「Release 後是否真的存在方向延續？」

---

# 必須統計

## 1. Release 後 continuation probability

例如：

```text
release 後 10 分鐘
是否持續同方向？
```

---

## 2. 哪一腿較容易被 stop？

檢查：

是否存在：

* liquidity bias
* spread asymmetry

---

## 3. Release 後 expectancy

檢查：

```text
裸單 trailing
是否真的有 edge？
```

---

# 預期結果

可能出現：

| 結果            | 解讀                     |
| ------------- | ---------------------- |
| release 後常延續  | breakout hypothesis 成立 |
| release 後常反轉  | noise release          |
| 永遠同一腿 stop    | liquidity bias         |
| 幾乎不觸發 release | stop 太寬                |
| release 過於頻繁  | stop 太窄                |

---

# Phase 0 成功標準

不是：

# PnL 最大化

而是：

## 恢復交易流量

例如：

```text
每週至少 10~30 次 release event
```

---

# 最終目的

建立：

# Minimal Tradable Spread Engine（MTSE）

之後才逐步加入：

* adaptive release
* spread analytics
* regime intelligence
* dynamic hedge ratio
* spread momentum model

避免再次進入：

# complexity explosion。

