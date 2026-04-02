# tw-trading-unified

台股期貨 + 選擇權整合交易系統。單一 Shioaji session，避免 Too Many Connections。

## 重大修正紀錄 (Critical Fixes)

### 2026-04-02 系統穩定性與準確性優化
- **[Core] 修正 Numba 回測併發 Bug**：移除 `vectorized.py` 中的 `parallel=True`，解決交易模擬因狀態競爭導致的結果錯誤。
- **[Core] 點值標準化修正**：統一全系統點值設定，明確標註 **小台 (MTX) = 50 元**，確保風險管理與損益計算精確。
- **[Strategy] 優化停損/停利模擬**：在回測中納入開盤價 (Gap) 判斷，避免因跳空導致的最大回撤低估。
- **[System] 交易日邏輯 (Trading Day) 對齊**：
    - 修正指標計算與日誌命名，統一以 **凌晨 05:00** 作為日期切換點（支援夜盤跨日）。
    - 修正 `indicators.py` 中的 VWAP 與日統計量，確保跨午夜數據連續。
- **[Monitor] 冷卻時間邏輯修正**：修復 `cooldown` 計數器在無信號時凍結的 Bug，確保空倉時每棒正常遞減。
- **[UI] 儀表板魯棒性提升**：
    - 優化 `dashboard.py` 時間戳解析，支援多種時區格式。
    - 增加自動尋找最新日誌檔案的 Fallback 機制，解決無當日檔案時顯示空白的問題。

## 架構

```
core/shioaji_session.py          # Singleton Shioaji 登入（全進程共用）
strategies/
  futures/monitor.py             # TMF Squeeze + Trend Breakout + bull_align guard
  options/monitor.py             # TXO V2 Swing 選擇權策略
  options/logs/                  # 選擇權 indicator / ledger / equity
main.py                          # 啟動入口：tick+bidask 分發 → os.execv 重啟 → 健康檢查
ui/dashboard.py                  # Streamlit 整合儀表板 (port 8500)
config/
  futures.yaml                   # 期貨策略參數
  options_strategy.yaml          # 選擇權策略參數（V1/V2/V3 modes）
  risk_global.yaml               # 全域資金分配
scripts/                         # 回測腳本
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

# 4. 啟動 dashboard
python3 -m streamlit run ui/dashboard.py --server.port 8500
```

## 用 tmux 執行

```bash
tmux new-session -d -s unified
# window 0: monitor
tmux send-keys -t unified:0 "cd ~/Documents/mylin102/tw-trading-unified && python3 main.py" Enter
# window 1: dashboard
tmux new-window -t unified:1
tmux send-keys -t unified:1 "cd ~/Documents/mylin102/tw-trading-unified && python3 -m streamlit run ui/dashboard.py --server.port 8500 --server.headless true" Enter
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

### 凌晨不斷線

- **kbars API** 凌晨不回傳數據 → 自動 fallback 到 tick-built bars
- **BidAsk 訂閱** 凌晨持續推送五檔報價 → IV/Greeks 即時計算
- **啟動順序**：先 `find_best_contracts()` → 再 `subscribe()` → 避免 race condition

## 交易模式控制

各策略的 live/paper 由各自 config 決定：
- `config/futures.yaml` → `live_trading: true/false`
- `config/options_strategy.yaml` → `live_trading: true/false`

CLI 的 `--dry-run` 是安全開關，完全不登入 broker，兩個都強制 paper。

## Dashboard (port 8500)

| Tab | 功能 |
|-----|------|
| 📈 總覽 | 期貨/選擇權即時指標、今日 PnL、指數雙軸走勢圖 |
| 🔵 期貨 | Close / Score / 趨勢(🟢多頭/🔴空頭) / Squeeze 狀態、交易記錄、PnL 曲線 |
| 🟠 選擇權 | MTX / Score / 趨勢 / IV、Trade Ledger、PnL 曲線 |
| ⚙️ 設定 | 參數即時調整、LIVE/PAPER 切換、資金分配、模擬 Reset |

夜盤跨日（00:00~05:00）自動顯示前一天的數據。

## Auto-Restart

使用 `os.execv` 重啟整個 process，確保 Shioaji session 完全乾淨：
1. `.restart` flag 或 thread 死亡 → 停止 monitors
2. `logout()` → 等 10 秒 → `os.execv` 重啟 process
3. 每 30 秒健康檢查（`api.list_positions`），session 壞掉自動重啟

## 策略邏輯

### 期貨 TMF
- 多週期 Squeeze（5m/15m/1h）+ Trend Breakout
- **Regime filter (mid) + bull_align guard**：多頭排列禁止做空，空頭排列禁止做多
- **EMA12/36**（1h/3h 趨勢判斷，回測最佳）
- TP1 分批停利 + Trailing Stop + ATR 動態停損
- VWAP 出場
- Tick-based bar builder：凌晨 kbars 不可用時自動切換

### 選擇權 TXO (V2 Swing)
- **月選合約（≥14 天 DTE）**，降低 theta 衰減
- 進場：Score ≥ 60 + **Squeeze Fire 或 Score ≥ 90** + **bull_align guard**
- **Cooldown 3 bars**：出場後不立即重新進場
- 停損 20% + **TP1 80% 分批** + **Trailing Stop 15%**
- 持倉上限 7 天，DTE < 3 天強制出場
- **即時 IV/Greeks**：py_vollib 從 BidAsk 中價反推 implied volatility
- 權重：1h:40% / 15m:40% / 5m:20%（加重大級別）

### 選擇權 Modes

| Mode | 持倉 | 合約 | 特性 |
|------|------|------|------|
| V1 | daytrade | 近月 | 當日沖 |
| **V2** | **swing** | **月選** | **波段（目前使用）** |
| V3 | night | 近月 | 夜盤當沖 |

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
# 期貨 regime filter 比較
python3 scripts/backtest_regime_filter.py

# 期貨 EMA 週期比較
python3 scripts/backtest_ema_full.py

# 選擇權參數網格優化
python3 scripts/backtest_options_optimize.py

# 選擇權 V2 swing vs V3 night
python3 scripts/backtest_v2_swing.py

# 選擇權 IV 過濾
python3 scripts/backtest_iv_filter.py
```

## 來源 repo

| 策略 | 原 repo |
|------|---------|
| 期貨 TMF | [tw-futures-realtime](https://github.com/mylin102/tw-futures-realtime) |
| 選擇權 TXO | [tw-option-squeeze-trading](https://github.com/mylin102/tw-option-squeeze-trading) |

## ⚠️ 免責聲明

本專案僅供學術與模擬研究，不構成任何投資建議。
