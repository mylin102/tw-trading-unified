# 台股交易策略文檔 (Stock Trading Strategy)

**日期**: 2026-05-31
**版本**: v1.5
**作者**: Gemini CLI

本文檔整理了 `tw-trading-unified` 系統中現有的台股交易進場與出場策略。系統主要針對零股（Odd-Lot）與整股交易進行優化，結合了技術面、籌碼面與型態分析。

---

## 1. 進場策略 (Entry Strategies)

目前系統支援多種可插件式進場策略，核心邏輯位於 `strategies/stocks/entry_strategies.py`。

### 1.1 零股偵察兵 (Scout Strategy) - **推薦策略**
*   **核心邏輯**: 先以極小量零股試單，獲利確認趨勢後再加碼整股。
*   **階段 1 (IDLE → SCOUT)**:
    *   Squeeze Fired (指標釋放)。
    *   成交量大於 20 均量的 1.5 倍。
    *   MACD 動能向上。
    *   大盤濾網：多頭市場需 `mom_state >= 2`；空頭市場需 `mom_state >= 3`。
*   **階段 2 (SCOUT → MAIN)**:
    *   獲利 > 0.8% 且 < 2.5%。
    *   持倉超過 1 根 K 線。
    *   股價高於 VWAP 或 EMA Fast，或突破強度 > 0.35。

### 1.2 CANSLIM 突破策略
*   **核心邏輯**: 追蹤高成長潛力股的關鍵型態突破。
*   **進場條件**:
    *   識別出「杯中帶把 (Cup with Handle)」或「雙底 (Double Bottom)」型態。
    *   帶量突破 Pivot 點 (成交量 > 20 均量 1.5 倍)。
    *   大盤非空頭市場。
    *   (選配) 基本面過濾：EPS/營收成長 > 20%, ROE > 15%。

### 1.3 投信作帳波段 (IT Window Dressing)
*   **核心邏輯**: 跟隨法人（投信）連續買超建倉的標的。
*   **進場條件**:
    *   投信連續 2-3 日買超。
    *   均線多頭排列 (Close > MA20 > MA60)。
    *   股價位於月線 (MA60) 之上。

### 1.4 均值回歸 (Mean Reversion)
*   **核心邏輯**: 捕捉股價超跌後的反彈。
*   **進場條件**:
    *   **基礎版**: 股價跌破布林帶下軌 (BB Lower)。
    *   **增強版**: 結合多時間框架 (Multi-TF)，確認 15m/60m 趨勢非極端空頭。
    *   **KD版**: K < 20 (超賣) + ADX < 30 (非強趨勢) + 股價在 EMA200 之上。

### 1.5 其他技術面策略
*   **EMA 回踩 (EMA Pullback)**: 趨勢中股價拉回收在 EMA Slow (MA60) 附近，且 ADX > 20 確認趨勢。
*   **布林下軌反彈 (BB Bounce)**: 觸及下軌且 MACD 柱狀體翻正或回升。
*   **動能突破 (Momentum Breakout)**: 突破今日高點且漲幅 > 2%，成交量需放大 2 倍。

---

## 2. 出場與風險管理 (Exit & Risk Management)

出場邏輯由 `strategies/stocks/exit_enhancer.py` 與 `StockMonitor` 協同管理。

### 2.1 基礎停損停利
*   **硬停損**: 進場價 - 2~3% (依配置調整)。
*   **硬停利**: 進場價 + 12.5% (依配置調整)。

### 2.2 移動停利 (Trailing Stop)
*   **啟動門檻**: 當獲利達到 `trailing_activation_pct` (預設 1.2%) 時啟動。
*   **觸發條件**: 價格從波段最高點回落達 `trailing_drawdown_pct` (預設 1.0%) 時執行全平倉。

### 2.3 時間與環境出場
*   **收盤出場 (Market Close)**: 每日 13:25 後強制平倉（避免隔夜風險）。
*   **持倉時間限制**: 超過 `max_holding_bars` (預設 30 根 5m K線) 仍未達成目標時出場。
*   **結構性轉弱**: 
    *   SCOUT 階段：若獲利跌破 -1% 或 MACD 翻負且跌破 VWAP，立即 Fail-fast 出場。

### 2.4 空頭防禦機制 (Bear Defense)
*   當大盤位於 60EMA 之下或觸及 `max_daily_loss` 時啟動。
*   **限制**: 最大持倉數降低（如 3 檔降為 1 檔），或全面暫停進場。

---

## 3. 策略配置參數 (Config)

策略參數統一在 `config/stocks.yaml` 中管理：
- `entry_score`: 進場評分門檻 (預設 15)。
- `atr_mult`: ATR 止損倍數 (預設 1.8)。
- `capital_per_trade`: 每筆交易預算。
- `total_portfolio_budget`: 總投資組合預算。

---
*本文檔由交易系統自動生成，策略邏輯以 Python 源碼為準。*
