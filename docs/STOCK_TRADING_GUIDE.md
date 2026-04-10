# 🇹🇼 Taiwan Stock Trading Guide (Odd-Lot Focus)

本文件說明 `tw-trading-unified` 專案中新增的台股模組架構、零股 API 調用規範以及開發路徑。

---

## 🏗️ 系統架構

台股模組整合於現有架構中，共用 Shioaji Session 以避免登入限制。

*   **Scanner (`strategies/stocks/scanner.py`)**:
    使用 **「整股」** 歷史數據進行多週期指標分析 (MTF)。日線負責型態識別，5分K負責進場驗證。
*   **Pattern Engine (`strategies/stocks/pattern_engine.py`)**:
    核心幾何型態偵測引擎，識別「杯中帶把」與「雙重底」，並計算 Pivot Point。
*   **Monitor (`strategies/stocks/monitor.py`)**:
    實時監控 Watchlist，緩存每日掃描結果，並在價格突破 Pivot 時執行交易。
*   **Execution**:
    下單時強制設定 `order_lot=sj.constant.StockOrderLot.Odd`，支援精確到「股」的自動交易。

---

## 📈 CANSLIM 突破策略

系統實作了 William O'Neil 的 CANSLIM 核心技術邏輯：

### 1. 形態識別 (Base Building)
*   **杯中帶把 (Cup with Handle)**: 
    *   深度限制：12% - 40%。
    *   把手要求：回檔不超過杯身的 15%，且長度需大於 3 天。
*   **雙重底 (Double Bottom)**: (Wave 2 實作)。

### 2. 進場點 (The Pivot)
*   **價格觸發**: 當前價格必須 **帶量突破** 把手高點 (Pivot Point)。
*   **量能確認**: 突破時的成交量必須大於過去 20 日平均量的 **1.4 倍**。

### 3. 市場濾網 (Market Direction)
*   **M 邏輯**: 只有在大盤 (TMF) 指標顯示非強空頭（例如 Close > EMA60）時，才允許個股開倉。

---

## 🛡️ 風險控管政策 (Risk Management)

### 1. 硬性止損 (Hard Stop Loss)
*   **觸發條件**: 當個股虧損達到 **-3%** (預設) 時，系統將立即執行 `Action.Sell`。
*   **執行優先級**: 這是系統中最高優先級的任務，無視其他指標。

### 2. 時間止損 (Time-Based Exit)
*   **觸發條件**: 每日 **13:20**。
*   **目的**: 為了避免隔日大幅跳空風險，系統預設會在收盤前清空短線個股部位。

### 3. 處置股過濾 (Disposition Stock Filtering)
*   **原則**: 系統 **絕對不參與** 處置股交易。
*   **技術檢查**: 下單前自動檢查 `contract.notice`。若非 `Normal` 狀態，則立即跳過。
*   **原因**: 避免 API 自動化環境下無法預收券資導致下單失敗，並避開流動性不佳的風險。

### 3. 資金隔離
*   **保證金門檻**: 單筆下單前會自動根據預算計算可買「股數」，不超額交易。

---

## 🛠️ 開發與測試規範

### 1. 數據分析 (Analysis) — 使用整股數據
```python
# 抓取整股歷史 K 線進行技術分析 (不使用 odd_lot 參數)
kbars = api.kbars(contract, start="YYYY-MM-DD")
```

### 2. 即時監控與下單 (Execution)
```python
# 取得行情 (注意：最新版 API 可能不支援在 snapshots 帶 odd_lot 參數)
snapshot = api.snapshots([contract])

# 下單時指定零股屬性
order = api.Order(
    ...,
    quantity=shares, # 單位：股
    order_lot=sj.constant.StockOrderLot.Odd
)
```

---

## 🚀 快速啟動 Dry Run

執行以下腳本來驗證選股掃描與模擬交易邏輯：
```bash
python3 scripts/dry_run_stocks.py
```
