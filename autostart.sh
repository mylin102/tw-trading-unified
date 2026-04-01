#!/bin/bash
# tw-trading-unified 啟動腳本（crontab 用）
# 檢查是否已在跑，沒有才啟動

UNIFIED_DIR="/Users/mylin/Documents/mylin102/tw-trading-unified"
LOG="$UNIFIED_DIR/logs/unified.log"
mkdir -p "$UNIFIED_DIR/logs"

if pgrep -f "tw-trading-unified/main.py" > /dev/null; then
    echo "[$(date)] already running" >> "$LOG"
    exit 0
fi

# 確保舊進程不在
pkill -f "tw-futures-realtime.*autostart" 2>/dev/null
pkill -f "live_options_squeeze_monitor" 2>/dev/null

cd "$UNIFIED_DIR"

# 用 tmux 啟動
tmux has-session -t unified 2>/dev/null && tmux kill-session -t unified
tmux new-session -d -s unified
tmux send-keys -t unified:0 "cd $UNIFIED_DIR && python3 main.py 2>&1 | tee $LOG" Enter

# Dashboard
tmux new-window -t unified:1
tmux send-keys -t unified:1 "cd $UNIFIED_DIR && python3 -m streamlit run ui/dashboard.py --server.port 8500 --server.address 127.0.0.1 --server.headless true 2>&1 | tee logs/dashboard.log" Enter

echo "[$(date)] unified started" >> "$LOG"
