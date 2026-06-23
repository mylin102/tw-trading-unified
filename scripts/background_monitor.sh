#!/bin/bash
# GSD 交易守護神 (The Guardian)
# 功能：監控 main.py|stock_runner.py，崩潰時自動清理並重啟

REPORT="logs/health_report.log"
RESTART_LOG="logs/auto_restart.log"
MAX_RESTARTS=10
RESTART_COUNT=0

echo "--- 守護神啟動 $(date) ---" >> $REPORT

while true; do
    # 1. 檢查主程式是否在跑
    if ! pgrep -f "main.py|stock_runner.py" > /dev/null; then
        if [ $RESTART_COUNT -lt $MAX_RESTARTS ]; then
            echo "🚨 [$(date '+%H:%M:%S')] 偵測到交易引擎停止！執行自動救援..." >> $REPORT
            echo "--- 自動重啟第 $((RESTART_COUNT+1)) 次 ---" >> $RESTART_LOG

            # 💡 GSD: 僅清理引擎，保留 Dashboard
            chmod +x scripts/stop_engine_only.sh
            ./scripts/stop_engine_only.sh >> $RESTART_LOG 2>&1

            # 等待 5 秒緩衝
            sleep 5

            # 2026-06-23 Gemini CLI: limit CPU usage of main.py and stock_runner.py to 50%
            nohup ./scripts/python3-cpulimit.sh main.py >> logs/unified.log 2>&1 &
            nohup ./scripts/python3-cpulimit.sh scripts/runners/stock_runner.py >> logs/stock_runner.log 2>&1 &

            RESTART_COUNT=$((RESTART_COUNT+1))
            echo "✅ 救援完成。等待系統穩定..." >> $REPORT
            sleep 30
        else
            echo "❌ [FATAL] 已達到最大重啟次數 ($MAX_RESTARTS)，守護神放棄救援。" >> $REPORT
            exit 1
        fi
    fi

    # 2. 檢查數據是否更新 (檢測指標文件是否有變化)
    # 如果指標文件超過 10 分鐘沒動，也視為異常
    LATEST_BAR_TIME=$(stat -f %m logs/market_data/TMF_$(date +%Y%m%d)_PAPER_indicators.csv 2>/dev/null)
    NOW=$(date +%s)
    if [ ! -z "$LATEST_BAR_TIME" ]; then
        AGE=$((NOW - LATEST_BAR_TIME))
        if [ $AGE -gt 900 ]; then
             echo "⚠️ [$(date '+%H:%M:%S')] 數據停滯 $AGE 秒，執行引擎重啟..." >> $REPORT
             chmod +x scripts/stop_engine_only.sh
             ./scripts/stop_engine_only.sh >> $RESTART_LOG 2>&1
             sleep 2
             # 2026-06-23 Gemini CLI: limit CPU usage of main.py to 50%
             nohup ./scripts/python3-cpulimit.sh main.py >> logs/unified.log 2>&1 &
        fi
    fi

    sleep 30
done
