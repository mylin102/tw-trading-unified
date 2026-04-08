# Project: tw-trading-unified 架構演進計畫

## Milestone 0: 改版前基線 (2026-04-08) ✅

### 現狀架構
- **三合一入口**: `main.py` (期貨+選擇權+股票 threads)。
- **投信作帳策略 (New)**: 已實作 `it_buy_rolling_count` 代理指標與單元測試。
- **容器化 (New)**: 已建立基於官方 `sinotrade/shioaji` 的 `Dockerfile` 與 `docker-compose.yml`。

### 已知問題
- **故障不隔離**: 股票模組掛掉會影響期貨/選擇權交易。
- **環境依賴**: 本地開發環境與雲端 Linux 環境存在微小差異。
- **手動營運**: 依賴 `autostart.sh` 與 `cron`，缺乏現代化的容器編排。

---

## Milestone 0.5: 容器化驗證 (目標: 2026-04-09) 🏃

### 目標
確保系統能在 Docker 容器內 100% 正常運作，解決 Linux-amd64 相容性問題。

### 執行波次
- [x] **Wave 1: Docker 基礎建置**: 建立 `Dockerfile` (Shioaji Base) 與 `requirements.txt`。
- [x] **Wave 2: 多服務定義**: 撰寫 `docker-compose.yml` 定義 `trading-monitor` 與 `dashboard`。
- [ ] **Wave 3: 容器內測試 (CL3)**: 在容器內執行 `pytest` 與 `scripts/backtest_it_strategy.py`。
    - *指導原則*: 驗證 `numba` 編譯與時區 `Asia/Taipei` 是否正確。

---

## Milestone 1: Stock Monitor 獨立化 (目標: 2026-04-12)

### 目標
將 Stock Monitor 拆成獨立服務，實現真正的故障隔離。

### 架構變更 (Docker-Centric)
- `docker-compose.yml` 拆分為三個服務：`futures-monitor`, `options-monitor`, `stock-runner`。

### 執行波次
- [ ] **Wave 1: 建立 stock_runner.py**: 獨立 Shioaji login + 異常自癒。
- [ ] **Wave 2: 核心解耦**: 移除 `main.py` 中的股票 thread，改為純期權入口。
- [ ] **Wave 3: 狀態監控 (gstack)**: 在 Dashboard 加入服務存活燈號（讀取 CSV timestamp 判定）。

---

## Milestone 1.5: GCP 遷移驗證 (目標: 2026-04-15) 🚀

### 目標
從本地 MacBook 遷移至 GCP asia-east1，實現 24/7 高可用交易。

### 執行波次
- [ ] **Wave 1: Phase 0 可行性驗證**: 在 GCP Compute Engine 啟動容器並測試 Ping 延遲。
- [ ] **Wave 2: 安全升級 (gstack /cso)**: 將 `.env` 密鑰遷移至 **GCP Secret Manager**。
- [ ] **Wave 3: 資料持久化**: 設定 GCS 掛載或定期備份 `exports/trades` 帳本。

---

## Milestone 2: 選股信號源整合 (目標: 2026-04-19)

### 目標
由 `squeeze-backtest` 提供每日 Watchlist，實現「選股」與「執行」分離。

### 執行波次
- [ ] **Wave 1: 標準化 JSON 輸出**: 定義 `watchlist_tw.json` 規格。
- [ ] **Wave 2: 動態載入**: `stock_runner.py` 啟動時自動讀取最新 JSON。

---

## 驗收標準 (V-Model Compliance)
1. **P0**: 所有修改前後必須通過 `python3 -m pytest tests/ -v` (128+ tests)。
2. **P0**: 停止 `stock-runner` 服務不得導致 `futures-monitor` 斷線。
3. **P1**: GCP 執行環境下的網路延遲應穩定在 50ms 以內。
4. **P1**: 密鑰不得以純文字形式存在於 Docker Image 或環境變數中。
