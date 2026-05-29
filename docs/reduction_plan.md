# tw-trading-unified 降維重構計劃（Reduction Plan）

## 目標

目前系統的主要問題不是：

* 缺少策略
* 缺少 observability
* 缺少 regime intelligence

而是：

# 系統 complexity 已超過 signal throughput

造成：

* 六週無交易
* Gate 疊加
* Regime 卡死
* Diagnostics 遠大於 execution

因此本計劃的核心目標：

# 從「複雜但不交易」

# 回到「簡單但可交易」

---

# 核心原則

## 1. Tradability First

任何策略：

# 必須先能穩定產生交易

之後才談：

* Sharpe
* DD
* Regime optimization
* Meta routing

---

## 2. Baseline Before Intelligence

先建立：

# Minimal Tradable System（MTS）

再逐步增加 intelligence。

不是反過來。

---

## 3. Remove Before Add

未來 iteration：

* 優先刪除 gate
* 不再優先新增 gate

---

# 第一階段：建立 Minimal Tradable System（MTS）

## 目標

建立：

# 「裸體也能活」的最小交易系統

---

# 保留項目

## Entry

僅保留：

# ORB breakout

例如：

* 開盤區間突破
* 5m ORB
* 15m ORB

只能選一個版本。

---

## Exit

只保留：

### ATR stop

### ATR trailing

例如：

stop = 1.5 \times ATR

以及：

trail = 2.0 \times ATR

---

## Risk

只保留：

* max loss/day
* max concurrent position
* hard kill switch

---

# 全部移除項目

## Regime System

移除：

* SQUEEZE
* WEAK
* STRETCHED
* BEAR
* TRANSITION

全部 disable。

---

## Router

移除：

* strategy ranking
* candidate sorting
* policy arbitration
* score routing

---

## Gate

移除：

* theta gate
* spread stale gate
* momentum state gate
* squeeze fire gate
* NO_EXTREME_LEVEL
* ORB_BUILDING gate

---

## Cross Market

移除：

* TX/TMF cross confirmation
* ETF resonance
* bond confirmation
* hedge demand

---

## Meta Logic

移除：

* scout policy
* release policy
* dynamic strategy enable/disable
* router meta learning

---

# 第二階段：恢復交易能力

## KPI

第一目標：

# 恢復「正常交易頻率」

---

## 強制目標

### 每週：

* 至少 10~20 trades

若低於：

# 視為 over-filtering

不是市場問題。

---

## 核心觀察

### 不是看 Sharpe

而是：

| 指標                  | 目標 |
| ------------------- | -- |
| trade frequency     | 恢復 |
| signal continuity   | 恢復 |
| execution stability | 穩定 |
| runtime crash       | 0  |
| fill consistency    | 穩定 |

---

# 第三階段：建立 Baseline Expectancy

## 核心目的

回答：

# 「裸體系統本身是否有 edge？」

---

## 必須量測

| 指標                  | 說明       |
| ------------------- | -------- |
| Win rate            | 勝率       |
| Avg win             | 平均獲利     |
| Avg loss            | 平均虧損     |
| Profit factor       | PF       |
| Max DD              | 最大回撤     |
| Trade frequency     | 頻率       |
| Regime distribution | 不同市場狀態表現 |

---

## 最重要

### 檢查：

# 是否由少數大 trend 貢獻全部收益

如果是：

代表：

# 系統本質是 trend follower

那麼：

未來不應過度 filter chop。

---

# 第四階段：Gate Telemetry System

## 所有 gate 必須可量測

未來：

任何 gate：

都必須輸出：

| Gate         | Kill Rate |
| ------------ | --------- |
| theta        | ?         |
| spread stale | ?         |
| weak regime  | ?         |
| ORB building | ?         |

---

# 原則

## 任何 gate：

若 kill rate > 50%

必須重新檢討。

---

# 第五階段：重新導入複雜度（Optional）

只有當：

# MTS 已穩定獲利

才允許：

逐步增加 intelligence。

---

# 新增順序（重要）

## 只能一次增加一項

例如：

1. 加 regime
2. 觀察 2 週
3. 若有效再保留
4. 再加下一項

---

# 禁止

## 一次新增：

* router
* regime
* cross-market
* volatility model
* scout system

否則：

# 無法知道誰有效。

---

# 建議保留的核心架構

## 保留

* observability
* structured logging
* contract tests
* crash recovery
* ledger durability
* replay capability

---

## 降維

* strategy intelligence
* meta-routing
* multi-regime gating

---

# 架構重構方向

## 現在

```text
Market
  ↓
Regime
  ↓
Router
  ↓
Policy
  ↓
Candidate Filter
  ↓
Gate
  ↓
Meta Gate
  ↓
Signal
  ↓
Execution
```

---

## 重構後

```text
Market
  ↓
Signal
  ↓
Risk Check
  ↓
Execution
```

---

# 最終目標

不是：

# 建立「最聰明」的系統

而是：

# 建立「能長期存活且持續交易」的系統

---

# 核心哲學

## 好策略：

即使裸體，

# 也應該能交易。

---

## 壞策略：

需要：

* 20 個 gate
* 5 個 regime
* 3 層 router

才敢下單。

---

# 建議實施順序

| Week    | 任務                          |
| ------- | --------------------------- |
| Week 1  | Disable 全部 regime/router    |
| Week 1  | 建立 MTS                      |
| Week 2  | 恢復交易頻率                      |
| Week 3  | 建立 baseline expectancy      |
| Week 4  | Gate telemetry              |
| Week 5+ | 一次只加一個 intelligence feature |

---

# 最後一句

目前 tw-trading-unified 最缺的：

不是：

* intelligence
* features
* complexity

而是：

# breathing room。

