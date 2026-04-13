<!-- generated-by: gsd-doc-writer -->
# Installation Guide | 安裝指南

This document provides step-by-step instructions to set up the Taiwan Trading Unified environment.
本文件提供設置 Taiwan Trading Unified 環境的逐步說明。

---

## 1. Prerequisites | 前置需求

### Runtime & Core | 運行環境與核心
- **Python**: 3.10 or higher (Recommended: 3.12)
- **TA-Lib**: C++ Library (Technical Analysis Library)

### OS-Specific Requirements | 作業系統特定需求

#### macOS
```bash
# Install TA-Lib using Homebrew
brew install ta-lib
```

#### Linux (Ubuntu/Debian)
```bash
# Install build essentials and dependencies
sudo apt-get update
sudo apt-get install -y build-essential wget python3-dev libatlas-base-dev gfortran pkg-config cmake

# Install TA-Lib from source
wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz
tar -xzf ta-lib-0.4.0-src.tar.gz
cd ta-lib/
./configure --prefix=/usr
make
sudo make install
cd .. && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz
```

#### Windows
1. Download the TA-Lib MSVC header/library from [ta-lib.org](https://ta-lib.org/hdr_dw.html).
2. Or use unofficial pre-compiled wheels: `pip install TA-Lib-Precompiled`.

---

## 2. Setup Steps | 設置步驟

### Clone Repository | 複製儲存庫
```bash
git clone <repository-url>
cd tw-trading-unified
git checkout main
```

### Install Python Dependencies | 安裝 Python 依賴
```bash
pip install -r requirements.txt
pip install TA-Lib pandas_ta
```

---

## 3. Manual Configuration | 手動配置 (Crucial/關鍵)

The following files are git-ignored for security and must be created manually in the project root.
為了安全起見，以下檔案已被 git 忽略，必須在專案根目錄手動建立。

### .env
Create a `.env` file for Shioaji API credentials and CA certificate.
建立一個 `.env` 檔案用於存放 Shioaji API 憑證與 CA 證書。

```env
SHIOAJI_API_KEY=YOUR_API_KEY
SHIOAJI_SECRET_KEY=YOUR_SECRET_KEY
SHIOAJI_PERSON_ID=YOUR_PERSON_ID
SHIOAJI_CA_PATH=/path/to/your/ca/folder
SHIOAJI_CA_NAME=your_ca_file.pfx
SHIOAJI_CA_PASSWD=YOUR_CA_PASSWORD
```

### config.json (Fallback/備援)
Provide a backup credential file `config.json` if required by legacy modules.
如果舊模組需要，請提供備援憑證檔案 `config.json`。

```json
{
  "api_key": "YOUR_API_KEY",
  "secret_key": "YOUR_SECRET_KEY",
  "person_id": "YOUR_PERSON_ID",
  "ca_path": "/path/to/ca",
  "ca_name": "ca.pfx",
  "ca_passwd": "YOUR_CA_PASSWORD"
}
```

---

## 4. Data Initialization | 資料初始化

Prepare the historical Parquet/CSV database and ensure data continuity.
準備歷史 Parquet/CSV 資料庫並確保資料連續性。

```bash
# Update and repair historical TMF data
python3 check_and_update_data.py
```
*This script will attempt to log in to Shioaji to download missing bars for the current day.*
*此腳本將嘗試登入 Shioaji 以下載當天缺失的 K 棒。*

---

## 5. Verification | 驗證

Ensure the system is correctly installed before live execution.
在進行實盤執行前，確保系統已正確安裝。

### Run Core Tests | 執行核心測試
```bash
python3 -m pytest tests/ -v
```

### Dry-run Main Engine | 核心引擎模擬運行
```bash
python3 main.py --dry-run
```
*Dry-run verifies all strategy plugins and monitors without logging into the broker.*
*模擬運行可在不登入券商的情況下驗證所有策略插件與監控器。*

---

## 6. Launching | 啟動系統

### Automatic Monitor | 自癒型自動啟動
Recommended for production environments. Starts the Dashboard and Core Monitors with auto-restart logic.
生產環境推薦使用。啟動儀表板與核心監控器，並具備自動重啟邏輯。
```bash
bash autostart.sh
```

### Interactive Launcher | 互動式啟動器
Useful for manual control and debugging.
適用於手動控制與除錯。
```bash
bash start_trading_system.sh
```

---
**Engineering Excellence for Data-Driven Trading.**
<!-- generated-by: gsd-doc-writer -->
