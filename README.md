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

# 2. 兩個都 paper（不登入 broker）
python3 main.py --dry-run

# 3. 登入 broker，但兩個都 paper（由 config 控制）
python3 main.py

# 4. 只讓選擇權 live，期貨 paper
# 修改 config/options_strategy.yaml → live_trading: true
python3 main.py --futures-paper

# 5. 兩個都 live
# 修改兩個 yaml → live_trading: true
python3 main.py
```

## 交易模式控制

兩層控制，CLI 優先覆蓋 config：

| CLI 參數 | 期貨 | 選擇權 |
|----------|------|--------|
| `--dry-run` | paper（不登入） | paper（不登入） |
| `--futures-paper` | paper | 看 config |
| `--options-paper` | 看 config | paper |
| 無參數 | 看 config | 看 config |

Config 設定：
- `config/futures.yaml` → `live_trading: true/false`
- `config/options_strategy.yaml` → `live_trading: true/false`

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
