#!/bin/bash
# tw-trading-unified gstack 加固版管理腳本
# 1. 隔離核心：期貨/選擇權 與 股票 分開執行
# 2. 環境自癒：啟動前自動補齊依賴
# 3. 徹底清理：重啟前殺死所有殘留 C++ 資源
# 4. macOS 優化：減少 "Python quit unexpectedly" 彈窗

UNIFIED_DIR="/Users/mylin/Documents/mylin102/tw-trading-unified"
PYTHON_EXEC="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
mkdir -p "$UNIFIED_DIR/logs"
cd "$UNIFIED_DIR"

echo "[$(date)] gstack Supervisor initializing..." >> logs/unified.log

# --- Layer 1: Pre-flight (環境自癒) ---
$PYTHON_EXEC -m pip install -q rich streamlit pandas pyyaml shioaji >> logs/unified.log 2>&1

# --- Layer 2: Clean up (徹底清理) ---
# macOS specific: kill all related processes gently first
pkill -15 -f "main.py" 2>/dev/null  # SIGTERM first
sleep 10  # Give main.py finally block time (8s cleanup) before SIGKILL
pkill -15 -f "streamlit" 2>/dev/null  # SIGTERM streamlit too
sleep 3  # Buffer for C++ resource cleanup
pkill -9 -f "main.py" 2>/dev/null  # Force kill if still alive (should not happen)
pkill -9 -f "streamlit" 2>/dev/null
sleep 2  # Final buffer before restart

# --- Layer 3: Launch Dashboards (哨兵監控台) ---
tmux has-session -t unified 2>/dev/null || tmux new-session -d -s unified

# Trading Dashboard (Port 8500)
tmux select-window -t unified:1 2>/dev/null || tmux new-window -t unified:1 -n "trading"
tmux send-keys -t unified:1 "$PYTHON_EXEC -m streamlit run ui/dashboard.py --server.port 8500 --server.headless true" Enter

# Backtest Dashboard (Port 8501)
tmux select-window -t unified:2 2>/dev/null || tmux new-window -t unified:2 -n "backtest"
tmux send-keys -t unified:2 "$PYTHON_EXEC -m streamlit run ui/backtest_dashboard.py --server.port 8501 --server.headless true" Enter

# --- Layer 4: Main Loop (核心自癒) ---
while true; do
    echo "[$(date)] 🚀 Launching Trading Core (Futures/Options)..." >> logs/unified.log

    # 執行主程式（我們現在把 main.py 定義為期貨核心）
    $PYTHON_EXEC "$UNIFIED_DIR/main.py" 2>&1 | tee -a logs/unified.log

    EXIT_CODE=$?
    echo "[$(date)] ⚠️ Core exited with code $EXIT_CODE. Re-booting in 15s..." >> logs/unified.log
    
    # macOS specific: longer buffer between restarts to reduce C++ crash dialog
    sleep 15
done
