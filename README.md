# tw-trading-unified

台股期貨 + 選擇權整合交易系統。單一 Shioaji session，避免 Too Many Connections。

## 架構

```
core/shioaji_session.py      # Singleton Shioaji 登入（全進程共用一個 session）
strategies/
  futures/monitor.py          # TMF 微台指 Squeeze 策略
  options/monitor.py          # TXO 選擇權 Squeeze 策略
main.py                       # 啟動入口：單一 session → tick 分發 → 雙 thread
config/
  futures.yaml                # 期貨策略參數
  options_strategy.yaml       # 選擇權策略參數
```

## 快速開始

```bash
# 1. 設定環境變數
cp .env.example .env   # 填入 Shioaji API Key

# 2. 不登入 broker，純 paper 測試
python3 main.py --dry-run

# 3. 登入 broker，live/paper 由各自 config 決定
python3 main.py
```

## 交易模式控制

各策略的 live/paper 由各自 config 決定：
- `config/futures.yaml` → `live_trading: true/false`
- `config/options_strategy.yaml` → `live_trading: true/false`

CLI 的 `--dry-run` 是安全開關，完全不登入 broker，兩個都強制 paper。

⚠️ 目前兩個都設為 `live_trading: false`（paper mode）。

## 用 tmux 執行

```bash
tmux new-session -d -s unified
tmux send-keys -t unified "cd /path/to/tw-trading-unified && python3 main.py" Enter
tmux attach -t unified
```

## 來源 repo

| 策略 | 原 repo |
|------|---------|
| 期貨 TMF | [tw-futures-realtime](https://github.com/mylin102/tw-futures-realtime) |
| 選擇權 TXO | [tw-option-squeeze-trading](https://github.com/mylin102/tw-option-squeeze-trading) |

## ⚠️ 免責聲明

本專案僅供學術與模擬研究，不構成任何投資建議。
