#!/bin/bash
# tw-trading-unified 守護啟動腳本
# 職責：在 main.py 退出或崩潰時自動重新啟動，確保 C++ 底層資源完全釋放。

UNIFIED_DIR="/Users/mylin/Documents/mylin102/tw-trading-unified"
LOG="$UNIFIED_DIR/logs/unified.log"
DASH_LOG="$UNIFIED_DIR/logs/dashboard.log"
mkdir -p "$UNIFIED_DIR/logs"

echo "[$(date)] Supervisor script started." >> "$LOG"

# 1. 啟動 Dashboard (如果沒在跑)
if ! pgrep -f "streamlit run ui/dashboard.py" > /dev/null; then
    echo "[$(date)] Starting Dashboard..." >> "$LOG"
    tmux has-session -t unified 2>/dev/null || tmux new-session -d -s unified
    tmux new-window -t unified:1 -n "dashboard" 2>/dev/null
    tmux send-keys -t unified:1 "cd $UNIFIED_DIR && python3 -m streamlit run ui/dashboard.py --server.port 8500 --server.address 127.0.0.1 --server.headless true 2>&1 | tee $DASH_LOG" Enter
fi

# 2. 主監控循環 (無限重啟)
while true; do
    echo "[$(date)] Launching main.py..." >> "$LOG"
    
    # 確保舊進程清理乾淨
    pkill -f "main.py" 2>/dev/null
    
    # 在 tmux 視窗 0 執行主程式
    tmux has-session -t unified 2>/dev/null || tmux new-session -d -s unified
    tmux select-window -t unified:0 2>/dev/null || tmux new-window -t unified:0 -n "monitor"
    
    # 執行並等待結束
    python3 "$UNIFIED_DIR/main.py" 2>&1 | tee -a "$LOG"
    
    echo "[$(date)] main.py exited with code $?. Waiting 15s for C++ cleanup before restart..." >> "$LOG"
    sleep 15
done
