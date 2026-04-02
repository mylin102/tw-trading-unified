# tw-trading-unified

台股期貨 + 選擇權整合交易系統。單一 Shioaji session，避免 Too Many Connections。

### 2026-04-02 策略與安全性重大升級 (v2)
- **[Strategy] Squeeze Failure Counter 策略**：新增均值回歸反向策略，回測 PF=1.95（原 Breakout PF=1.02）
- **[Strategy] Auto-Regime 切換**：根據 bullish_align 翻轉頻率自動判斷趨勢/盤整，切換 Breakout/Counter 模式
- **[Strategy] 參數優化**：ATR SL 1.5x、VWAP exit 啟用、Counter ATR SL 2.0x（vectorbt 網格回測最佳）
- **[Safety] 保證金檢查**：下單前查詢 `api.margin()` 權益數，不足額自動擋單
- **[Safety] 持倉恢復**：重啟時從 `api.list_positions()` 還原真實持倉，防止重複開單
- **[Safety] VWAP exit 隔離**：Counter 模式的 VWAP 出場不影響 Breakout 持倉
- **[UI] Dashboard 自動刷新**：30 秒 auto-refresh + Monitor 運行狀態指示燈
- **[Fix] CSV 欄位對齊**：`_save_bar` 對齊既有 CSV header，修復夜盤數據錯位
- **[Fix] `lookback` 參數 bug**：移除 `calculate_futures_squeeze` 不接受的參數
- **[Doc] Shioaji API 參考文件**：`docs/SHIOAJI_API_REFERENCE.md`

### 2026-04-02 系統架構與穩定性升級 (v1)
- **[Arch] 外部守護進程 (Supervisor)**：`autostart.sh` 外部循環監控，15 秒延遲重啟
- **[Core] API 調用頻率限制**：kbars 5 分鐘頻率限制
- **[Logic] 交易日日誌對齊**：凌晨 05:00 切換
- **[Fix] Greeks 計算型別修正**：強制 `float` 轉換

## 架構

```
core/shioaji_session.py          # Singleton Shioaji 登入（全進程共用）
strategies/
  futures/monitor.py             # TMF Breakout + Counter (auto-regime) + margin check
  options/monitor.py             # TXO V2 Swing 選擇權策略
  options/live_options_squeeze_monitor.py  # 選擇權核心引擎
  options/logs/                  # 選擇權 indicator / ledger / equity
main.py                          # 啟動入口：tick+bidask 分發 → 健康檢查
autostart.sh                     # 外部守護進程（斷線自動重啟）
ui/dashboard.py                  # Streamlit 儀表板 (port 8500, 30s auto-refresh)
config/
  futures.yaml                   # 期貨策略參數（含 counter_mode）
  options_strategy.yaml          # 選擇權策略參數（V1/V2/V3 modes）
  risk_global.yaml               # 全域資金分配
scripts/                         # 回測腳本
docs/                            # 設計文件 + API 參考
data/                            # 歷史數據（回測用）
logs/market_data/                # 期貨 indicator CSV
```

## 快速開始

```bash
# 1. 設定環境變數
cp .env.example .env   # 填入 Shioaji API Key + DASHBOARD_PASSWORD

# 2. 不登入 broker，純 paper 測試
python3 main.py --dry-run

# 3. 登入 broker，live/paper 由各自 config 決定
python3 main.py

# 4. 用 supervisor 啟動（推薦，斷線自動重啟）
bash autostart.sh
```

## 即時數據架構

```
Shioaji API
  ├─ on_tick_fop_v1 ──→ tick_dispatcher ──→ futures monitor (tick bar builder)
  │                                     └─→ options monitor (MTX price update)
  ├─ on_bidask_fop_v1 → bidask_dispatcher → options monitor (bid/ask mid-price)
  │                                        ├─ MTX: 標的價 (S)
  │                                        ├─ TXO Call: 權利金 + IV 反推
  │                                        └─ TXO Put: 權利金 + IV 反推
  └─ api.kbars() ────→ 歷史 K 棒（日盤可用，凌晨 fallback 到 tick bars）
```

## 交易模式控制

各策略的 live/paper 由各自 config 決定：
- `config/futures.yaml` → `live_trading: true/false`
- `config/options_strategy.yaml` → `live_trading: true/false`

CLI 的 `--dry-run` 是安全開關，完全不登入 broker，兩個都強制 paper。

## 安全機制（Live 下單保護）

| 層級 | 機制 | 說明 |
|------|------|------|
| 策略層 | `max_positions: 1` | 限制最大持倉口數 |
| 資金層 | `_margin_sufficient()` | 進場前查 `api.margin()` 權益數，扣 20% reserve |
| 恢復層 | `_recover_position_from_api()` | 重啟時從 API 還原持倉，防重複開單 |
| 交易所層 | 保證金不足拒單 | 最終防線 |
| ⚠️ 待實作 | Safety Stop | 進場後預掛停損單，斷線時交易所執行 |

## Dashboard (port 8500)

| Tab | 功能 |
|-----|------|
| 📈 總覽 | 期貨/選擇權即時指標、今日 PnL、指數雙軸走勢圖 |
| 🔵 期貨 | Close / Score / 趨勢 / Squeeze 狀態、交易記錄、PnL 曲線 |
| 🟠 選擇權 | MTX / Score / 趨勢 / IV、Trade Ledger、PnL 曲線 |
| ⚙️ 設定 | 參數即時調整、LIVE/PAPER 切換、資金分配 |

- 每 30 秒自動刷新（`streamlit-autorefresh`）
- Header 顯示 Monitor 運行狀態（🟢 Running / 🔴 Stopped）
- 夜盤跨日（00:00~05:00）自動顯示前一天的數據

## 策略邏輯

### 期貨 TMF — 雙模式自動切換

#### Breakout 模式（趨勢盤）
- 多週期 Squeeze（5m/15m/1h）+ Trend Breakout
- Regime filter (mid) + bull_align guard
- EMA 12/36 趨勢判斷

#### Counter 模式（盤整盤）— 新增
- 偵測 Squeeze Fire 後 5 bars 內突破失敗
- 失敗條件：未創新高/低 + 動能反轉 + VWAP 拒絕
- 反向進場，VWAP 回歸出場
- 回測 PF=1.95, MaxDD=-7.2%（vs Breakout PF=1.02, MaxDD=-25.8%）

#### Auto-Regime 切換
- `_is_ranging_regime()`：近 20 bars bullish_align 翻轉 ≥4 次 → 盤整 → Counter
- 否則 → 趨勢 → Breakout

#### 出場機制
- ATR 1.5x 動態停損
- VWAP 出場（Breakout + Counter）
- TP1 分批停利 + Trailing Stop
- Cooldown 3 bars

### 選擇權 TXO (V2 Swing)
- 月選合約（≥14 天 DTE），降低 theta 衰減
- 進場：Score ≥ 60 + Squeeze Fire + bull_align guard
- 停損 20% + TP1 80% 分批 + Trailing Stop 15%
- 持倉上限 7 天，DTE < 3 天強制出場
- 即時 IV/Greeks：py_vollib 從 BidAsk 中價反推

### 選擇權 Modes

| Mode | 持倉 | 合約 | 特性 |
|------|------|------|------|
| V1 | daytrade | 近月 | 當日沖 |
| **V2** | **swing** | **月選** | **波段（目前使用）** |
| V3 | night | 近月 | 夜盤當沖 |

## 最佳參數（2026 Q1 回測）

### Breakout
| 參數 | 值 |
|------|-----|
| entry_score | 20 |
| atr_multiplier | 1.5 |
| exit_on_vwap | true |
| regime_filter | mid |

### Counter
| 參數 | 值 |
|------|-----|
| confirm_bars | 5 |
| atr_sl_mult | 2.0 |
| exit_on_vwap | true |

## 資金分配

```yaml
# config/risk_global.yaml
allocation:
  futures:
    max_margin_pct: 0.40
  options:
    max_margin_pct: 0.40
account:
  margin_reserve_pct: 0.20
```

## 回測腳本

```bash
# vectorbt 網格回測（Breakout + Counter 雙模式）
python3 scripts/backtest_vbt_grid.py

# Squeeze Failure Counter vs Breakout 比較
python3 scripts/backtest_squeeze_failure.py

# 期貨 regime filter / EMA 比較
python3 scripts/backtest_regime_filter.py
python3 scripts/backtest_ema_full.py

# 選擇權參數優化 / V2 vs V3 / IV 過濾
python3 scripts/backtest_options_optimize.py
python3 scripts/backtest_v2_swing.py
python3 scripts/backtest_iv_filter.py
```

## 文件

| 文件 | 說明 |
|------|------|
| `docs/SHIOAJI_API_REFERENCE.md` | Shioaji API 快速參考（登入/下單/行情/帳務） |
| `docs/SQUEEZE_FAILURE_STRATEGY.md` | Counter 策略設計提案 |
| `docs/DASHBOARD_UI_DESIGN.md` | Dashboard UI 設計 |

## 來源 repo

| 策略 | 原 repo |
|------|---------|
| 期貨 TMF | [tw-futures-realtime](https://github.com/mylin102/tw-futures-realtime) |
| 選擇權 TXO | [tw-option-squeeze-trading](https://github.com/mylin102/tw-option-squeeze-trading) |

## ⚠️ 免責聲明

本專案僅供學術與模擬研究，不構成任何投資建議。
