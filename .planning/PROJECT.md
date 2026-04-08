# Project: tw-trading-unified 架構演進計畫

## Milestone 0: 改版前基線 (2026-04-08 Snapshot)

### 現狀架構
```
tw-trading-unified/
├── main.py                    # 三合一入口 (期貨+選擇權+股票 threads)
├── autostart.sh               # cron 啟動 + while-true 自癒 loop
├── strategies/
│   ├── futures/monitor.py     # FuturesMonitor (tick-driven, TMF)
│   ├── options/
│   │   ├── monitor.py         # OptionsMonitor wrapper
│   │   ├── live_options_squeeze_monitor.py  # 核心 (Greeks, ThetaGang)
│   │   └── theta_gang.py      # Iron Condor / Spread 策略
│   └── stocks/monitor.py      # StockMonitor (scan-driven, 15 檔)
├── config/                    # 4 個 YAML 設定檔
├── ui/
│   ├── dashboard.py           # Streamlit :8500 (4 tabs)
│   └── backtest_dashboard.py  # Streamlit :8501
└── tests/                     # 128 tests
```

### 量化指標
| 指標 | 數值 |
|---|---|
| Python 檔案數 | 123 |
| 測試數量 | 128 passed |
| Monitor 類別 | 4 (Futures, Options, OptionsSmartMonitor, Stock) |
| Shioaji 版本 | 1.3.2 |
| 共用 Shioaji session | 1 (所有 monitor 共用) |
| Cron jobs | 4 (日盤/夜盤啟動 + 2 dashboard watchdog) |
| Dashboard ports | 8500 (trading), 8501 (backtest) |

### 已知問題 (2026-04-08 修復記錄)
1. ✅ `record_signal_snapshot` NameError — strike/dte_years 未初始化
2. ✅ Stock Monitor thread 死亡無法恢復 — 缺 outer try/except
3. ✅ `'Shioaji' has no attribute 'trades'` — hasattr guard
4. ✅ `sj.constant.StockNotice` 不存在 — getattr 替代
5. ✅ `NaN to int` 投信指標 — fillna(0)
6. ⚠️ `strategy_it_window_dressing()` 參數不匹配 — 待修
7. ⚠️ Shioaji session 斷線 → 三個 monitor 全部受影響（架構問題）

### 核心痛點
- **故障不隔離**: Stock Monitor 掛了 → main.py health check 持續告警 → 重啟影響期貨/選擇權
- **重啟代價高**: 修任何一個 monitor 的 bug 都要重啟整個 trading core
- **Context Rot**: main.py 同時管 Shioaji login、tick dispatch、3 個 thread 的生命週期
- **squeeze-backtest 與 StockMonitor 職責重疊**: 兩邊都在做選股篩選

---

## Milestone 1: Stock Monitor 獨立化

### 目標
Stock Monitor 從 main.py thread 拆成獨立 process，期貨/選擇權不受股票模組影響。

### 架構變更
```
Before:                          After:
main.py                          main.py
├── FuturesMonitor (thread)      ├── FuturesMonitor (thread)
├── OptionsMonitor (thread)      └── OptionsMonitor (thread)
└── StockMonitor   (thread)
                                 stock_runner.py (獨立 process)
                                 └── StockMonitor
                                     └── 自己的 Shioaji session 或 共用 pool
```

### 執行波次

#### Wave 1: 建立 stock_runner.py 獨立入口
- [ ] 從 main.py 抽出 StockMonitor 啟動邏輯
- [ ] stock_runner.py: 獨立 Shioaji login + StockMonitor.run()
- [ ] 自帶 while-true 自癒 loop（參考 autostart.sh 模式）
- [ ] 驗證: stock_runner.py 能獨立跑，寫出 indicator CSV

#### Wave 2: main.py 瘦身
- [ ] 移除 main.py 中 StockMonitor thread 相關程式碼
- [ ] 移除 `st_t.is_alive()` 檢查和 "Stock Monitor died" 告警
- [ ] main.py while loop 只管 `ft.is_alive() and ot.is_alive()`
- [ ] 驗證: main.py 重啟不影響 stock_runner.py，反之亦然

#### Wave 3: 運維整合
- [ ] autostart.sh 同時啟動 main.py + stock_runner.py
- [ ] crontab 加 stock_runner watchdog（獨立於 main.py）
- [ ] dashboard.py 不需改（讀的是 CSV，跟 process 無關）
- [ ] 驗證: kill stock_runner → 期貨/選擇權不受影響 → watchdog 自動重啟

### 驗收標準
- [ ] `kill stock_runner.py` → 期貨 dashboard 資料不中斷
- [ ] `kill main.py` → 股票 dashboard 資料不中斷
- [ ] 128 tests 全過
- [ ] 兩個 process 可獨立重啟，無互相依賴

---

## Milestone 2: squeeze-backtest → 選股信號源

### 目標
squeeze-backtest 產出 watchlist，tw-trading-unified 只負責執行。

### 架構變更
```
squeeze-backtest/
├── cron_daily_tasks.sh          # 每日 06:00 選股
└── output/
    └── watchlist_tw.json        # {"date": "20260408", "tickers": ["2330", ...]}

tw-trading-unified/
└── stock_runner.py
    └── 讀取 watchlist_tw.json   # 不再自己做 _filter_watchlist_by_strength()
```

### 執行波次
- [ ] Wave 1: squeeze-backtest 輸出標準化 watchlist JSON
- [ ] Wave 2: stock_runner.py 讀取 JSON 取代 config/stocks.yaml 的靜態 watchlist
- [ ] Wave 3: 移除 StockMonitor._filter_watchlist_by_strength()（職責歸 squeeze-backtest）

### 驗收標準
- [ ] squeeze-backtest 每日 06:00 產出 watchlist_tw.json
- [ ] stock_runner.py 讀取並交易，不做重複篩選
- [ ] 選股邏輯只在 squeeze-backtest 維護

---

## Milestone 3: Shioaji Session Pool (上 live 前)

### 目標
多 process 共用 Shioaji 連線，避免多次 login 被擋。

### 方案評估
| 方案 | 優點 | 缺點 |
|---|---|---|
| A: 各自 login | 最簡單 | 永豐可能限制同時連線數 |
| B: Unix socket 共享 | 故障隔離好 | 需要寫 IPC 層 |
| C: 主 process 代理 | 集中管理 | 回到單點故障 |

### 決策: 先用方案 A 驗證
永豐 Shioaji 允許同帳號多 session（已確認 simulation mode 可以）。
如果 live mode 被限制，再切換到方案 B。

---

## 時程估計
| Milestone | 預估工時 | 前置條件 |
|---|---|---|
| M0 基線 ✅ | 完成 | — |
| M1 Stock 獨立化 | 2-3 小時 | M0 |
| M2 選股信號源 | 1-2 小時 | M1 |
| M3 Session Pool | 需要時再做 | M1 + live 上線前 |
