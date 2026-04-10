# 🇹🇼 Taiwan Stock Trading Guide (Odd-Lot Focus)

本文件說明 `tw-trading-unified` 專案中新增的台股模組架構、零股 API 調用規範以及開發路徑。

---

## 🏗️ 系統架構

台股模組整合於現有架構中，共用 Shioaji Session 以避免登入限制。

*   **Scanner (`strategies/stocks/scanner.py`)**:
    使用 **「整股」** 歷史數據進行指標分析，以確保信號穩定性。
*   **Monitor (`strategies/stocks/monitor.py`)**:
    實時監控 Watchlist，使用 **「零股」** 快照獲取精確的可成交價。
*   **Execution**:
    下單時強制設定 `order_lot=sj.constant.StockOrderLot.Odd`，支援精確到「股」的自動交易。

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
