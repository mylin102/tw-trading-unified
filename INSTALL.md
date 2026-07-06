# Taiwan Trading Unified - Deployment & Execution Guide
# 台灣交易系統部署與執行指南

本指南說明如何在全新機器上快速部署並執行最新版本的交易系統。

---

## 🚀 1. 取得最新程式碼 (Sync Code)

```bash
# 複製儲存庫 (如果是第一次)
git clone https://github.com/mylin102/tw-trading-unified.git
cd tw-trading-unified

# 切換至最新開發分支 (Crucial)
git checkout feat/squeeze-stock-strategies
git pull origin feat/squeeze-stock-strategies
```

---

## 📦 2. 環境設置 (Environment Setup)

### Python 與套件
建議使用 Python 3.10+。
```bash
pip install -r requirements.txt
```

### TA-Lib 安裝 (必要)
- **macOS**: `brew install ta-lib`
- **Linux**: 請參考系統內的 `INSTALL.md` 原文進行編譯。
- **Windows**: 使用預編譯版本 `pip install TA-Lib-Precompiled`。

---

## 🔑 3. 憑證與金鑰 (Credentials - 手動操作)

由於安全因素，`.env` 與憑證檔案不會上傳至 Git。**請從舊機器手動複製以下內容至新機器：**

1.  **`.env` 檔案**：放置於專案根目錄。
2.  **憑證檔案 (`.pfx`)**：路徑需與 `.env` 中的 `SHIOAJI_CA_PATH` 一致。

---

## 📊 4. 數據初始化 (Data Initialization)

執行以下腳本確保歷史 K 線與個股資料完整：

```bash
# 1. 檢查並更新核心數據 (Futures/Options)
python3 check_and_update_data.py

# 2. 修補當日缺失的 K 棒 (如果是在盤中或盤後部署)
python3 scripts/maintenance/patch_today_kbars.py

# 3. 自動下載缺少的個股歷史數據
python3 scripts/auto_download_missing_stocks.py
```

---

## 🛠️ 5. 執行系統 (Execution)

### A. 完整啟動 (推薦)
使用自動化腳本同時啟動交易引擎與監控系統：
```bash
chmod +x start_trading_system.sh
./start_trading_system.sh
```

### B. 視覺化儀表板 (Dashboard)
啟動 Streamlit 介面以監控策略訊號與部位：
```bash
streamlit run ui/dashboard.py
```

### C. 系統狀態檢查
驗證所有背景進程是否正常運作：
```bash
./check_system_status.sh
```

---

## 🧪 6. 測試與驗證 (Verification)

在正式啟動前，建議執行冒煙測試：
```bash
# 執行所有單元測試
python3 -m pytest tests/ -v

# 執行模擬登入測試
python3 test_shioaji_simple.py
```

---
**💡 提示**：若遇到數據讀取錯誤，請確認 `data/taifex_raw/` 目錄下的 CSV 檔案是否已存在。
