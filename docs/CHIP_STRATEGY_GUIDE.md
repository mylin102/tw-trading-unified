# 📈 籌碼分點策略整合指南 (Chip Data Integration Guide)

本文件說明如何將「籌碼分析 (Chip Analysis)」整合進現有的 CANSLIM 策略中。

---

## 1. 核心邏輯 (Core Logic)

籌碼分點的核心概念是 **「跟隨主力」**。
我們不只看「量」，我們要看「是誰在買」。

### 策略整合公式
最終進場信心 = **CANSLIM 技術面 (70%)** + **籌碼面 (30%)**

- **技術面 (CANSLIM)**: 確認形態與突破 (杯帶把、Pivot 突破)。
- **籌碼面 (Chip)**: 確認突破是否有「法人/主力」真金白銀支持。

---

## 2. 籌碼評分機制 (Scoring System)

目前系統使用 **ChipAnalyzer** (`core/chip_analyzer.py`) 計算 0-10 分。

### 評分標準
| 分數 (Score) | 意義 | 定義 | 對應操作 |
|:---:|:---|:---|:---|
| **8 - 10** | 🔥 **強烈看多** | 主力連續 5 天買超，或今日爆量買超 | **加碼買入 (Scale In)** |
| **5 - 7** | ✅ **正面確認** | 主力淨買超，量價齊揚 | **正常買入 (Full Size)** |
| **3 - 4** | ⚠️ **觀望/中性** | 散戶行情，或主力小幅賣超 | **減碼買入 (Half Size)** |
| **0 - 2** | 🛑 **負面/危險** | 主力大量出貨，或量縮 | **放棄交易 (No Trade)** |

---

## 3. 程式碼實作 (Implementation)

在 `strategies/stocks/entry_strategies.py` (即時進場) 或 `scripts/backtest_canslim.py` (回測) 中加入籌碼過濾。

### 方法 A：硬過濾 (Hard Filter) - 推薦 ⭐⭐⭐
若籌碼分數低於門檻，直接放棄此訊號。

```python
from core.chip_analyzer import chip_analyzer

def strategy_canslim_with_chip(state, cfg):
    # 1. 原有的 CANSLIM 邏輯
    signal = strategy_stock_canslim_breakout(state, cfg)
    if not signal:
        return None

    # 2. 籌碼確認 (Chip Confirmation)
    ticker = state.get("ticker") # 假設 state 中有代號
    # 獲取分數 (Live Mode 會去爬取真實資料)
    chip_score = chip_analyzer.get_chip_score(ticker) 
    
    # 設定門檻 (例如：大於 5 分才進場)
    CHIP_THRESHOLD = 5.0 
    
    if chip_score < CHIP_THRESHOLD:
        print(f"🚫 Chip Score {chip_score} < {CHIP_THRESHOLD}. Signal Ignored.")
        return None # 放棄此訊號

    return signal
```

### 方法 B：動態停損 (Dynamic Stop Loss) - 進階 ⭐⭐
若籌碼分數高，可放寬停損；若分數低，縮緊停損。

```python
    # 計算停損距離
    base_stop_dist = 0.07 # 基準 7%
    
    if chip_score >= 8.0:
        # 主力強力護盤，停損放寬至 10%
        stop_loss = entry_price * (1 - 0.10)
    elif chip_score < 3.0:
        # 主力不在，嚴格防守，停損縮至 4%
        stop_loss = entry_price * (1 - 0.04)
    else:
        # 標準停損
        stop_loss = entry_price * (1 - base_stop_dist)
```

---

## 4. 資料來源設定 (Data Source Configuration)

由於真實的「券商分點」歷史資料極難免費取得，系統設計了雙模式：

### 模式 1: Live Mode (即時實盤)
*   **資料源**: 爬蟲抓取 Goodinfo.tw / 證交所。
*   **設定**: `ChipAnalyzer(mode="live")`
*   **行為**: 每次執行 `get_chip_score` 時會嘗試網路連線。
*   **注意**: 證交所 API 在盤後 17:00-19:00 更新，白天抓到的是昨日資料。

### 模式 2: Backtest Mode (歷史回測)
*   **資料源**: 量比代理 (Volume Ratio Proxy)。
*   **設定**: `ChipAnalyzer(mode="backtest")`
*   **原因**: 歷史分點資料需付費購買。回測時若用假資料會導致過度擬合 (Overfitting)。
*   **替代方案**: 使用「爆量 (Volume Breakout)」作為主力介入的代理指標。

---

## 5. 常見問題 (FAQ)

### Q1: 為什麼回測時沒有看到籌碼分數？
**A:** 因為預設是 `backtest` 模式，且目前預設回傳 0 或基於 Hash 的固定值，以避免隨機性干擾回測結果。建議回測階段先依賴技術面，實盤再依賴籌碼面。

### Q2: 如果爬蟲失敗怎麼辦？
**A:** 系統會自動 Catch Exception 並回傳 `0.0` 分。建議設定合理的 fallback 邏輯（如：回傳 5 分中性）。

### Q3: 可以指定特定主力分點嗎？
**A:** 可以。修改 `core/chip_analyzer.py` 中的 `KEY_BROKERS_KEYWORDS` 列表。
```python
KEY_BROKERS_KEYWORDS = ["摩根大通", "美林", "高盛"]
```
