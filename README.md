# tw-trading-unified

台股期貨 + 選擇權整合交易系統。單一 Shioaji session，避免 Too Many Connections。

## 架構

```
core/shioaji_session.py          # Singleton Shioaji 登入（全進程共用）
strategies/
  futures/monitor.py             # TMF Squeeze + Trend Breakout 策略
  options/monitor.py             # TXO 選擇權 Squeeze 策略
main.py                          # 啟動入口：單一 session → tick 分發 → 雙 thread → auto-restart
ui/dashboard.py                  # Streamlit 整合儀表板 (port 8500)
config/
  futures.yaml                   # 期貨策略參數
  options_strategy.yaml          # 選擇權策略參數
  risk_global.yaml               # 全域資金分配
```

## 快速開始

```bash
# 1. 設定環境變數
cp .env.example .env   # 填入 Shioaji API Key

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
tmux send-keys -t unified:0 "cd /path/to/tw-trading-unified && python3 main.py" Enter
# window 1: dashboard
tmux new-window -t unified:1
tmux send-keys -t unified:1 "cd /path/to/tw-trading-unified && python3 -m streamlit run ui/dashboard.py --server.port 8500 --server.headless true" Enter
```

## 交易模式控制

各策略的 live/paper 由各自 config 決定：
- `config/futures.yaml` → `live_trading: true/false`
- `config/options_strategy.yaml` → `live_trading: true/false`

CLI 的 `--dry-run` 是安全開關，完全不登入 broker，兩個都強制 paper。

Dashboard 設定 tab 可即時切換 LIVE/PAPER（double confirm），切換後自動重啟 monitor。

⚠️ 目前兩個都設為 `live_trading: false`（paper mode）。

## Dashboard (port 8500)

| Tab | 功能 |
|-----|------|
| 📈 總覽 | 期貨/選擇權即時指標、今日 PnL、指數雙軸走勢圖 |
| 🔵 期貨 | TMF 價格 + Score 走勢、交易記錄、PnL 曲線 |
| 🟠 選擇權 | MTX 價格 + Score 走勢、Trade Ledger、PnL 曲線 |
| ⚙️ 設定 | 參數即時調整、LIVE/PAPER 切換、資金分配、模擬 Reset |

## 資金分配

```yaml
# config/risk_global.yaml
allocation:
  futures:
    max_margin_pct: 0.40    # 0~80%，Dashboard slider 可調
  options:
    max_margin_pct: 0.40    # 0~80%
account:
  margin_reserve_pct: 0.20  # 安全墊，不可分配
```

規則：`futures + options + reserve <= 100%`

## Auto-Restart

Dashboard 切換 LIVE/PAPER 時：
1. 寫入 config YAML
2. 建立 `.restart` flag
3. `main.py` 偵測 flag → 停止 monitor → logout → 等 30 秒 → 重新啟動

## 策略邏輯

### 期貨 TMF
- 多週期 Squeeze（5m/15m/1h）+ Trend Breakout
- Regime filter（loose/mid/strict）
- TP1 分批停利 + Trailing Stop + ATR 動態停損
- VWAP 出場

### 選擇權 TXO
- 多週期 Squeeze 動能共振（Score ≥ 90）
- ATM Call/Put 買方
- DTE 進場前檢查 + Min DTE 出場
- EOD Panic/Passive 出場
- Black-Scholes 定價

## 來源 repo

| 策略 | 原 repo |
|------|---------|
| 期貨 TMF | [tw-futures-realtime](https://github.com/mylin102/tw-futures-realtime) |
| 選擇權 TXO | [tw-option-squeeze-trading](https://github.com/mylin102/tw-option-squeeze-trading) |

## ⚠️ 免責聲明

本專案僅供學術與模擬研究，不構成任何投資建議。
