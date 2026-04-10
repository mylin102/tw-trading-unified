# tw-trading-unified

台股期貨 + 選擇權 + 股票 整合交易系統。單一 Shioaji session，避免 Too Many Connections。

### 2026-04-10 CANSLIM Stock Integration & System Stabilization (v5)
- **[Strategy] CANSLIM Pattern Engine**：新增 `pattern_engine.py` 幾何型態偵測模組
  - 支援「杯中帶把 (Cup with Handle)」與「雙重底 (Double Bottom)」識別
  - 使用 smoothed `argrelextrema` 與 parabolic 擬合提高識別精確度
- **[Arch] MTF Stock Pipeline**：`StockScanner` 支援雙軌掃描
  - 日線 (1d)：進行長期型態分析與 Pivot Point 計算
  - 5分K (5m)：進行即時成交量驗證與進場執行
- **[Stability] 數據連續性修復**：解決 TMF 夜盤成交量為零導致的指標停滯問題
  - 透過 MTX (小台) Virtual Ticks 驅動 TMF K棒生成
  - 修復 `api.kbars` 頻率限制問題（自研 Pandas 高性能 resampling）
- **[Risk] 開盤尖刺保護**：實裝 `opening_grace_mins` (5m) 與 `entry_premium_limit` (250)
- **[UI] Dashboard v2.0**：新增三資產 (期貨/選擇權/台股) 整合監控牆與 Round-Trip 交易日誌
- **[Test] Geometric Test Suite**：新增 `test_pattern_engine.py` 包含幾何型態產生器

### 2026-04-07 macOS 穩定性 + SDD/V-Model 加固 (v4)
- **[Stability] macOS 優雅關閉**：Signal handlers (SIGTERM/SIGINT) + `_shutdown_event` 協調
- **[Test] V-Model Level 1**：17 個新測試覆蓋 macOS 安全性 (`test_macos_safety.py`)
- **[Doc] SDD 補充**：`docs/SDD_MACOS_SAFETY.md` 完整設計文件

## 架構

```
core/shioaji_session.py          # Singleton Shioaji 登入（全進程共用）
strategies/
  futures/monitor.py             # TMF Breakout + Counter (auto-regime)
  options/monitor.py             # TXO V2 Swing 選擇權策略
  stocks/monitor.py              # 台股監控與零股執行（CANSLIM 支援）
  stocks/pattern_engine.py       # 幾何型態偵測引擎 (Cup with Handle / W Bottom)
  stocks/scanner.py              # MTF 掃描器 (Daily Base + 5m Execution)
main.py                          # 啟動入口：三資產數據分發
ui/dashboard.py                  # Streamlit 儀表板 (三資產整合)
config/
  futures.yaml                   # 期貨策略參數
  options_strategy.yaml          # 選擇權策略參數
  stocks.yaml                    # 台股策略參數（含 CANSLIM 設定）
```

## 快速開始

```bash
# 1. 設定環境變數
cp .env.example .env   # 填入 Shioaji API Key + DASHBOARD_PASSWORD

# 2. 下載歷史數據 (用於型態分析)
python3 strategies/stocks/downloader.py

# 3. 啟動交易系統
bash autostart.sh
```

## Dashboard (port 8500)

| Tab | 功能 |
|-----|------|
| 📈 總覽 | 三資產即時指標、今日 PnL、指數走勢圖 |
| 🔵 期貨 | TMF 指標、交易記錄、PnL 曲線 |
| 🟠 選擇權 | TXO 指標、IV/Greeks、PnL 曲線 |
| 🍎 台股 | 15 檔標的監控、CUP/W 型態識別、Pivot 標註 |
| ⚙️ 設定 | 參數即時調整、LIVE/PAPER 切換 |

## 策略邏輯

### 台股 Stocks — CANSLIM 突破策略
- **選股 (C/A)**：透過財報或籌碼 API 預篩強勢股 (Watchlist)
- **形態 (Base)**：`pattern_engine` 偵測日線級別「杯中帶把」或「雙底」
- **突破 (When)**：價格 > Pivot Point 且 成交量 > 20日均量 1.4倍
- **市場 (M)**：大盤 (TMF) 非空頭排列時才啟動

### 期貨 TMF — 雙模式自動切換
- **Breakout**：多週期 Squeeze + Trend Breakout
- **Counter**：偵測 Squeeze Fire 失敗後反向進場 (Mean Reversion)
- **Auto-Regime**：根據波動頻率自動切換模式

## 文件

| 文件 | 說明 |
|------|------|
| `docs/STOCK_TRADING_GUIDE.md` | **台股交易指南** (CANSLIM 實作細節) |
| `docs/ELITE_STRATEGIES.md` | 精英策略完整文檔 |
| `docs/LIVE_TRADING_GUIDE.md` | 實盤轉換指南 |
| `docs/CANSLIM_strategy.pdf` | CANSLIM 理論參考 |
| `docs/SDD.md` | 軟體設計文檔 |
| `docs/V_MODEL_TEST_PLAN.md` | 測試計畫 |

## ⚠️ 免責聲明

本專案僅供學術與模擬研究，不構成任何投資建議。
