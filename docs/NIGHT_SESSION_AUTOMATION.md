# Night Session Attribution Automation System

## 概述

夜盤 Attribution 自動化系統是一個完整的解決方案，用於在夜盤交易時段（15:00-05:00）自動追蹤策略曝光度、檢測飢餓現象、生成報告和優化策略優先級。

## 系統架構

### 核心組件

1. **Attribution 記錄系統** (`core/attribution_recorder.py`)
   - 追蹤 router 評估、策略信號和交易歸因
   - 自動刷新機制（緩衝區 1000 行，間隔 300 秒）
   - CSV 檔案輸出

2. **報告生成系統** (`scripts/attribution_report.py`)
   - 7 種報告類型：router 統計、飢餓分析、優先級影響、交易績效等
   - 視覺化圖表生成
   - CLI 介面支援篩選

3. **飢餓警報系統** (`scripts/starvation_alerts.py`)
   - 即時監控飢餓指數
   - 可配置閾值（預設 0.7）
   - 電子郵件通知支援

4. **策略重排序模擬器** (`docs/strategy_reorder_simulator.py`)
   - 模擬不同候選順序的預期結果
   - 基於歷史交易數據的期望 PnL 估計
   - 識別潛在的優先級優化機會

5. **夜盤自動化系統** (`scripts/night_automation.py`)
   - 整合所有組件的自動化監控
   - 夜盤時段自動啟用
   - 定期報告和警報檢查

6. **啟動器腳本** (`scripts/night_attribution_launcher.sh`)
   - 一鍵設置和啟動
   - 系統狀態檢查
   - Cron job 管理

## 快速開始

### 1. 初始設置

```bash
# 進入專案目錄
cd /Users/mylin/Documents/mylin102/tw-trading-unified

# 運行設置腳本
bash scripts/night_attribution_launcher.sh setup
```

這將：
- 檢查依賴項
- 創建必要目錄
- 生成配置文件
- 測試 attribution 系統

### 2. 測試系統

```bash
# 測試 attribution 系統
bash scripts/night_attribution_launcher.sh test

# 檢查系統狀態
bash scripts/night_attribution_launcher.sh status
```

### 3. 設置自動化（Cron Jobs）

```bash
# 設置自動化排程
bash scripts/night_attribution_launcher.sh cron

# 安裝 cron jobs
./cron/night_session/install_cron.sh

# 測試 cron jobs
./cron/night_session/test_cron.sh
```

### 4. 手動啟動監控

```bash
# 啟動夜盤監控
bash scripts/night_attribution_launcher.sh start

# 停止監控
bash scripts/night_attribution_launcher.sh stop

# 重啟監控
bash scripts/night_attribution_launcher.sh restart
```

## 夜盤排程

| 時間 | 動作 | 說明 |
|------|------|------|
| 14:55 | 啟動自動化 | 夜盤開始前 5 分鐘 |
| 15:00-05:00 | 夜盤活躍 | 自動化監控運行 |
| 每 15 分鐘 | 警報檢查 | 檢查飢餓指數 |
| 每小時 | 報告生成 | 生成 attribution 報告 |
| 每 2 小時 | 重排序模擬 | 模擬策略優先級優化 |
| 05:05 | 停止自動化 | 夜盤結束後 5 分鐘 |
| 05:10 | 每日總結 | 生成夜盤總結報告 |

## 目錄結構

```
tw-trading-unified/
├── data/
│   └── attribution/              # Attribution 數據
│       ├── router_evaluation_log.csv
│       ├── strategy_signal_log.csv
│       ├── trade_attribution_log.csv
│       └── archive/              # 歸檔數據
├── reports/
│   └── night_session/           # 夜盤報告
│       ├── attribution_YYYYMMDD_HHMM/
│       └── reorder_sim_YYYYMMDD_HHMM/
├── alerts/
│   └── night_session/           # 警報文件
│       └── alerts_YYYYMMDD_HHMMSS.json
├── logs/
│   ├── night_automation.log     # 自動化日誌
│   └── cron_night.log           # Cron job 日誌
├── config/
│   └── night_automation_config.json  # 配置文件
├── cron/
│   └── night_session/           # Cron job 腳本
│       ├── install_cron.sh
│       ├── remove_cron.sh
│       └── test_cron.sh
└── scripts/
    ├── night_automation.py      # 主自動化腳本
    ├── night_attribution_launcher.sh  # 啟動器
    ├── attribution_report.py    # 報告生成
    ├── starvation_alerts.py     # 警報系統
    └── attribution_backtest.py  # 測試腳本
```

## 配置選項

配置文件：`config/night_automation_config.json`

```json
{
    "attribution_dir": "/Users/mylin/Documents/mylin102/tw-trading-unified/data/attribution",
    "attribution_enabled": true,
    "attribution_buffer_size": 1000,
    "attribution_flush_interval": 300,
    "reports_dir": "/Users/mylin/Documents/mylin102/tw-trading-unified/reports/night_session",
    "alerts_dir": "/Users/mylin/Documents/mylin102/tw-trading-unified/alerts/night_session",
    "logs_dir": "/Users/mylin/Documents/mylin102/tw-trading-unified/logs",
    "starvation_threshold": 0.7,
    "priority_impact_threshold": 2.0,
    "low_evaluation_threshold": 10,
    "reorder_simulation_enabled": true,
    "reorder_interval_minutes": 120,
    "default_orders": [
        ["counter_vwap", "spring_upthrust", "kbar_feature"],
        ["kbar_feature", "counter_vwap", "spring_upthrust"],
        ["spring_upthrust", "kbar_feature", "counter_vwap"]
    ],
    "email_enabled": false,
    "email_recipient": "",
    "dashboard_enabled": true,
    "dashboard_port": 8500,
    "night_session_start_hour": 15,
    "night_session_end_hour": 5
}
```

## 關鍵指標

### 飢餓指數 (Starvation Index)

```
starvation_index = 1 - (評估次數 / 候選次數)
```

| 指數範圍 | 等級 | 建議行動 |
|---------|------|----------|
| 0.0-0.3 | 可接受 | 持續監控 |
| 0.3-0.7 | 中度 | 考慮調整優先級 |
| 0.7-1.0 | 嚴重 | 立即調整優先級 |

### 優先級影響 (Priority Impact)

```
priority_impact = 被壓制次數 / 贏得次數
```

| 影響值 | 意義 |
|--------|------|
| < 1.0 | 低壓制 |
| 1.0-2.0 | 中度壓制 |
| > 2.0 | 高壓制 |

## 使用範例

### 啟用 Attribution 記錄

```python
from core.attribution_recorder import AttributionRecorder

recorder = AttributionRecorder(
    output_dir="./data/attribution",
    buffer_size=1000,
    flush_interval_seconds=300
)

# 在 monitor 中使用
decision = monitor._route_signal(
    bar=bar_data,
    session_regime="WEAK",
    attribution_recorder=recorder
)
```

### 手動生成報告

```bash
# 生成 attribution 報告
python scripts/attribution_report.py \
  --input-dir ./data/attribution \
  --output-dir ./reports/night_session \
  --force

# 檢查飢餓警報
python scripts/starvation_alerts.py \
  --input-dir ./data/attribution \
  --threshold 0.7 \
  --email admin@example.com

# 運行重排序模擬
python docs/strategy_reorder_simulator.py \
  --input-dir ./data/attribution \
  --output-dir ./reports/reorder_sim \
  --order counter_vwap,spring_upthrust,kbar_feature \
  --order kbar_feature,counter_vwap,spring_upthrust
```

### 查看儀表板

```bash
# 啟動儀表板
streamlit run ui/dashboard.py

# 瀏覽器打開 http://localhost:8501
# 點擊 "Attribution" 標籤
```

## 故障排除

### 常見問題

**問題：沒有 attribution 數據**
```bash
# 檢查目錄
ls -la ./data/attribution/

# 檢查 CSV 檔案
head -5 ./data/attribution/router_evaluation_log.csv

# 啟用 attribution 記錄
python scripts/attribution_backtest.py --sample 100
```

**問題：自動化沒有啟動**
```bash
# 檢查系統狀態
bash scripts/night_attribution_launcher.sh status

# 檢查日誌
tail -f ./logs/night_automation.log

# 檢查是否夜盤時段
date '+%H:%M'
```

**問題：Cron jobs 沒有執行**
```bash
# 檢查 crontab
crontab -l

# 檢查 cron 日誌
tail -f ./logs/cron_night.log

# 測試手動執行
./cron/night_session/test_cron.sh
```

**問題：電子郵件警報失敗**
```bash
# 設置環境變數
export SMTP_SERVER="smtp.gmail.com"
export SMTP_PORT=587
export SMTP_USER="your-email@gmail.com"
export SMTP_PASSWORD="your-app-password"

# 更新配置文件
# 設置 email_enabled: true 和 email_recipient
```

### 日誌位置

- **自動化日誌**: `./logs/night_automation.log`
- **Cron job 日誌**: `./logs/cron_night.log`
- **Attribution 數據**: `./data/attribution/`
- **警報檔案**: `./alerts/night_session/`
- **報告輸出**: `./reports/night_session/`

## 維護指南

### 每日維護

1. **檢查警報**
   ```bash
   # 查看最新警報
   find ./alerts/night_session -name "*.json" -mtime -1 -exec cat {} \;
   ```

2. **檢查報告**
   ```bash
   # 查看最新報告
   ls -lt ./reports/night_session/
   ```

3. **檢查日誌**
   ```bash
   # 查看自動化日誌
   tail -100 ./logs/night_automation.log
   ```

### 每周維護

1. **清理舊數據**
   ```bash
   # 清理 30 天前的警報
   find ./alerts/night_session -name "*.json" -mtime +30 -delete
   
   # 清理 90 天前的報告
   find ./reports/night_session -type d -mtime +90 -exec rm -rf {} +
   
   # 歸檔舊 attribution 數據
   mv ./data/attribution/*.csv ./data/attribution/archive/ 2>/dev/null || true
   ```

2. **審查配置**
   ```bash
   # 檢查配置檔案
   cat ./config/night_automation_config.json
   
   # 根據需要調整閾值
   # starvation_threshold, priority_impact_threshold
   ```

### 每月維護

1. **性能優化**
   ```bash
   # 檢查數據大小
   du -sh ./data/attribution/
   
   # 調整緩衝區大小
   # attribution_buffer_size, attribution_flush_interval
   ```

2. **系統更新**
   ```bash
   # 更新 Python 套件
   pip install --upgrade pandas numpy plotly streamlit
   
   # 檢查系統依賴
   bash scripts/night_attribution_launcher.sh setup
   ```

## 進階功能

### 自定義策略順序

編輯配置文件中的 `default_orders`：

```json
"default_orders": [
    ["strategy_a", "strategy_b", "strategy_c"],
    ["strategy_b", "strategy_c", "strategy_a"],
    ["strategy_c", "strategy_a", "strategy_b"]
]
```

### 電子郵件通知

1. 設置環境變數：
   ```bash
   export SMTP_SERVER="smtp.gmail.com"
   export SMTP_PORT=587
   export SMTP_USER="your-email@gmail.com"
   export SMTP_PASSWORD="your-app-password"
   ```

2. 更新配置文件：
   ```json
   "email_enabled": true,
   "email_recipient": "admin@example.com"
   ```

### 儀表板整合

系統自動檢測儀表板狀態（port 8500）。如需自定義：

```json
"dashboard_enabled": true,
"dashboard_port": 8501  # 自定義端口
```

## 監控指標

### 系統健康指標

1. **Attribution 數據新鮮度**
   - 最後更新時間應在 1 小時內
   - 檔案大小應持續增長

2. **警報頻率**
   - 嚴重警報（🚨）應立即處理
   - 警告警報（⚠️）應每日審查

3. **報告完整性**
   - 每小時應生成報告
   - 報告應包含所有策略數據

### 性能指標

1. **處理延遲**
   - 數據處理應在 5 秒內完成
   - 報告生成應在 30 秒內完成

2. **資源使用**
   - 記憶體使用應低於 500MB
   - CPU 使用應低於 50%

## 緊急處理

### 系統故障

1. **自動化停止**
   ```bash
   # 檢查狀態
   bash scripts/night_attribution_launcher.sh status
   
   # 重啟系統
   bash scripts/night_attribution_launcher.sh restart
   
   # 檢查日誌
   tail -f ./logs/night_automation.log
   ```

2. **數據損壞**
   ```bash
   # 備份當前數據
   cp -r ./data/attribution ./data/attribution_backup_$(date +%Y%m%d_%H%M%S)
   
   # 清理損壞數據
   rm -f ./data/attribution/*.csv
   
   # 重啟系統
   bash scripts/night_attribution_launcher.sh restart
   ```

3. **Cron job 故障**
   ```bash
   # 移除所有 cron jobs
   ./cron/night_session/remove_cron.sh
   
   # 重新安裝
   ./cron/night_session/install_cron.sh
   
   # 測試
   ./cron/night_session/test_cron.sh
   ```

## 支援與聯絡

### 獲取幫助

1. **查看文檔**
   ```bash
   # 查看此文件
   cat docs/NIGHT_SESSION_AUTOMATION.md
   
   # 查看 attribution 文檔
   cat docs/ATTRIBUTION_MONITORING.md
   ```

2. **檢查示例**
   ```bash
   # 查看示例代碼
   grep -r "AttributionRecorder" examples/
   
   # 查看測試用例
   cat tests/core/test_attribution_recorder.py
   ```

3. **調試模式**
   ```bash
   # 啟用詳細日誌
   python scripts/night_automation.py --live --verbose
   
   # 檢查內部狀態
   python -c "
   from scripts.night_automation import NightSessionConfig
   config = NightSessionConfig()
   print(f'Config: {config.__dict__}')
   "
   ```

### 報告問題

遇到問題時，請提供：
1. 錯誤訊息
2. 相關日誌片段
3. 系統配置
4. 重現步驟

---

這個夜盤 Attribution 自動化系統現在已完全整合到您的交易系統中，提供全面的策略曝光度追蹤、飢餓檢測和優先級優化功能。系統設計為生產就緒，具有自動化監控、警報和豐富的報告功能。