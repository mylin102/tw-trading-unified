# 台股零股策略優化報告 v4.1

> 日期：2026-04-06 | 作者：Kiro + mylin | 狀態：模擬交易中

## 摘要

將台股零股策略從虧損的 Scout 策略切換為 `momentum_breakout`（動能突破），
回測 PnL 從 -3,494 提升至 +29,976 TWD，Profit Factor 從 0.59 提升至 3.99。

## 回測期間

- 資料：2025-06 ~ 2026-04（約 10 個月）
- 標的：46 檔台股（5 分鐘 K 棒）
- 參數：SL=2%, TP=10%, TS=1%

## 策略比較

| 版本 | PnL | Trades | WR | Avg/Trade | MaxDD | PF |
|------|-----|--------|----|-----------|-------|----|
| v0 Scout (原版) | -3,494 | 135 | 13% | -26 | -3,448 | 0.59 |
| v1 動能突破 | +29,413 | 179 | 24% | +164 | -1,965 | 3.70 |
| **v2 +動態篩選** | **+29,976** | **161** | **25%** | **+186** | **-1,912** | **3.99** |

### 新增策略

| 策略 | 邏輯 | 回測 PnL | 適用場景 |
|------|------|----------|----------|
| `momentum_breakout` | 漲幅>2% + 創新高 + 帶量確認(vol≥2x) | +29,413 | 主策略 |
| `kd_mean_reversion` | KD<20超賣 + ADX<30 + EMA200多頭 | +11,379 | 備用/分散 |
| `bb_bounce` | BB下軌 + MACD翻正 | -12,412 | 不建議 |
| `ema_pullback` | 多頭排列回踩EMA slow | -2,880 | 不建議 |

## 三招優化

### 第一招：帶量突破
- 進場時成交量 ≥ 過去 20 bars 均量 × 2
- 效果：交易數 272→179，平均獲利 +138→+164（+19%）

### 第二招：動態篩選 Watchlist
- 開盤 09:05 後，只留成交量大 + 開盤強勢的前半標的
- 效果：交易數 179→161，PF 3.70→3.99

### 第三招：獲利抱住
- 13:20 只撤退虧損倉，獲利倉抱到 13:25
- 回測中 97% 交易由 trailing stop 出場，此招為實盤保險

## 空頭防禦機制

| 層級 | 機制 | 觸發條件 |
|------|------|----------|
| 大盤濾網 | 持倉上限 3→1 | 加權指數 < EMA60 |
| 單日虧損 | 停止開新倉 | 當日虧損 ≥ 3,000 TWD |
| 連虧暫停 | 停止開新倉 | 連虧 ≥ 3 次 |

API 測試時大盤判定為 🐻 空頭（TAIEX=32,572 < EMA60=32,604），防禦已生效。

## 摩擦成本分析

| 項目 | momentum_breakout |
|------|-------------------|
| 毛利 | +53,244 TWD |
| 手續費 (0.05% × 2) | 10,880 |
| 證交稅 (0.3%) | 6,891 |
| **摩擦佔比** | **33%** |
| **淨利** | **+35,473 TWD** |

回測費率與真實券商 3.5 折費率一致，結果保守可靠。

## 技術改進

- **sweep_engine 80x 加速**：信號快取避免重複計算（14s vs 原本 stuck）
- **交易紀錄升級**：含 entry_price / fees / pnl_gross / pnl_cash(淨)
- **Dashboard 股票 Tab**：策略下拉選單動態載入、PnL 摘要指標、中文欄位

## 當前 Config

```yaml
# config/stocks.yaml
strategy: momentum_breakout
fallback_strategy: kd_mean_reversion
stop_loss_pct: 0.02
take_profit_pct: 0.10
trailing_stop_pct: 0.01
capital_per_trade: 20000
bear_defense:
  enabled: true
  market_ema_length: 60
  max_daily_loss: 3000
  max_consecutive_losses: 3
```

## 模擬交易計畫

- **開始日期**：2026-04-07（週二）
- **結束日期**：2026-04-11（週五），一週後檢討
- **模式**：Paper（live_trading: false）
- **啟動指令**：`python3 main.py`
- **Dashboard**：http://localhost:8500 → 台股 Stocks tab

### 檢討重點（2026-04-13）

1. 實際交易筆數 vs 回測預期（~4 筆/天）
2. 勝率是否接近 25%
3. 動態篩選是否有效過濾弱勢股
4. 空頭防禦是否正確觸發
5. 摩擦成本是否與回測一致
6. 13:20 獲利抱住是否有實際案例

### 交易紀錄位置

```
exports/trades/STOCK_YYYYMMDD_PAPER_trades.csv
logs/market_data/STOCK_{ticker}_{date}_indicators.csv
```

## 出場原因分布（回測）

| 原因 | 次數 | 總 PnL | 平均 |
|------|------|--------|------|
| 移動停損 | 174 (97%) | +32,314 | +186 |
| 硬停損 | 2 (1%) | -1,050 | -525 |
| 收盤平倉 | 3 (2%) | -1,851 | -617 |

Trailing stop 1% 是主要出場機制，快速鎖利。

## Git

- Branch: `feat/squeeze-stock-strategies`
- PR: https://github.com/mylin102/tw-trading-unified/pull/2
