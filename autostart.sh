#!/bin/bash
# tw-trading-unified 自愈型監控腳本 (v3.1)
# Contract:
# - PM2 is the sole supervisor for main.py
# - autostart.sh owns dashboards, stock runner, and maintenance only
# - main.py duplicate protection remains in main.py via /tmp/tw_trading_unified.pid

UNIFIED_DIR="/Users/mylin/Documents/mylin102/tw-trading-unified"
# 2026-06-23 Gemini CLI: limit CPU usage of python processes in autostart to 50%
PYTHON_EXEC="$UNIFIED_DIR/scripts/python3-cpulimit.sh"
LOG_DIR="$UNIFIED_DIR/logs"
mkdir -p "$LOG_DIR"
cd "$UNIFIED_DIR"

# ── Configuration ──
HEALTH_CHECK_INTERVAL=30       # 健康檢查間隔（秒）
MAX_CRASHES_PER_HOUR=3         # 每小時最大 crash 次數
LOG_RETENTION_DAYS=7           # 日誌保留天數

# ── GSD Singleton Lock ──
LOCKFILE="/tmp/tw_trading_unified.lock"
if ! mkdir "$LOCKFILE" 2>/dev/null; then
    # Check if lock holder is still alive
    if [ -f "$LOCKFILE/pid" ]; then
        OLD_PID=$(cat "$LOCKFILE/pid" 2>/dev/null)
        if ps -p "$OLD_PID" >/dev/null 2>&1; then
            echo "[$(date)] ⚠️ Another instance (PID=$OLD_PID) is running. Exiting." >> "$LOG_DIR/unified.log"
            exit 1
        else
            echo "[$(date)] 🧹 Removing stale lock (PID=$OLD_PID is dead)" >> "$LOG_DIR/unified.log"
            rmdir "$LOCKFILE" 2>/dev/null
            mkdir "$LOCKFILE" 2>/dev/null
        fi
    fi
fi
echo $$ > "$LOCKFILE/pid" 2>/dev/null
trap 'rmdir "$LOCKFILE" 2>/dev/null' EXIT

echo "[$(date)] 🛡️自愈型監控 v3.0 啟動..." >> "$LOG_DIR/unified.log"

# ======================== Layer 1: Log Rotation ========================
rotate_logs() {
    find "$LOG_DIR" -name "*.log" -mtime +$LOG_RETENTION_DAYS -delete 2>/dev/null
    # Compress old crash logs
    for f in "$LOG_DIR"/*crash*.log; do
        [ -f "$f" ] && [ ! -f "${f}.gz" ] && gzip "$f" 2>/dev/null
    done
}

# ======================== Layer 2: Pre-flight ========================
$PYTHON_EXEC -m pip install -q rich streamlit pandas pyyaml shioaji >> "$LOG_DIR/unified.log" 2>&1

# ======================== Layer 3: Clean Kill ========================
graceful_kill() {
    local pattern="$1"
    local name="$2"
    local pids=$(pgrep -f "$pattern" 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "[$(date)] 🛑 停止 $name (PIDs: $pids)..." >> "$LOG_DIR/unified.log"
        echo "$pids" | while read pid; do
            kill -15 "$pid" 2>/dev/null
        done
        sleep 5
        # Verify and force kill if still alive
        for pid in $pids; do
            if ps -p "$pid" >/dev/null 2>&1; then
                echo "[$(date)] ⚠️ $name PID=$pid still alive, force killing..." >> "$LOG_DIR/unified.log"
                kill -9 "$pid" 2>/dev/null
            fi
        done
        sleep 2
    fi
}

echo "[$(date)] 🧹 清理殘留進程..." >> "$LOG_DIR/unified.log"
graceful_kill "stock_runner.py" "股票"
graceful_kill "streamlit" "Dashboard"
rm -f /tmp/stock_runner_*.lock 2>/dev/null
echo "[$(date)] ✅ 清理完成" >> "$LOG_DIR/unified.log"

# ======================== Layer 4: Launch Services ========================
# Dashboards
if DASHBOARD_PORT=8500 DASHBOARD_APP=ui/dashboard.py PYTHON_EXEC="$PYTHON_EXEC" \
    "$UNIFIED_DIR/scripts/restart_dashboard.sh" >> "$LOG_DIR/unified.log" 2>&1; then
    echo "[$(date)] 📊 Dashboard restart script completed on :8500" >> "$LOG_DIR/unified.log"
else
    echo "[$(date)] ❌ Dashboard restart script failed on :8500" >> "$LOG_DIR/unified.log"
fi

nohup $PYTHON_EXEC -m streamlit run ui/backtest_dashboard.py \
    --server.port 8501 --server.address 127.0.0.1 --server.headless true \
    >> "$LOG_DIR/backtest_dashboard.log" 2>&1 &
echo "[$(date)] 📈 Backtest Dashboard PID=$! started on :8501" >> "$LOG_DIR/unified.log"

# Wait for dashboards
for port in 8500 8501; do
    for i in 1 2 3 4 5; do
        lsof -i :$port -sTCP:LISTEN >/dev/null 2>&1 && break
        sleep 3
    done
    if lsof -i :$port -sTCP:LISTEN >/dev/null 2>&1; then
        echo "[$(date)] ✅ Port $port is UP" >> "$LOG_DIR/unified.log"
    else
        echo "[$(date)] ❌ Port $port FAILED to start" >> "$LOG_DIR/unified.log"
    fi
done

# ======================== Layer 5: Crash Tracker ========================
CRASH_LOG="$LOG_DIR/crash_tracker.log"
CRASH_COUNT_FILE="/tmp/trading_crash_count"

record_crash() {
    local service="$1"
    local exit_code="$2"
    local runtime="$3"
    local timestamp=$(date +%s)
    
    echo "$timestamp $service $exit_code $runtime" >> "$CRASH_LOG"
    
    # Count crashes in last hour
    local one_hour_ago=$((timestamp - 3600))
    local recent_crashes=$(awk -v cutoff="$one_hour_ago" '$1 >= cutoff && $2 == "'"$service"'"' "$CRASH_LOG" | wc -l)
    
    if [ "$recent_crashes" -ge "$MAX_CRASHES_PER_HOUR" ]; then
        echo "[$(date)] 🚨 $service crashed $recent_crashes times in last hour! Cooling down for 10 minutes..." >> "$LOG_DIR/unified.log"
        sleep 600
        # Clear crash history after cooldown
        echo "" > "$CRASH_LOG"
    fi
}

# ======================== Layer 6: Service Loops ========================
echo "[$(date)] 🚀 啟動服務監控迴圈..." >> "$LOG_DIR/unified.log"

# --- Stock Monitor (日盤 08:30~13:45) ---
(
while true; do
    H=$(date +%H)
    if [ "$H" -ge 8 ] && [ "$H" -lt 14 ]; then
        STOCK_PID=$(pgrep -f "stock_runner.py" | head -1)
        if [ -z "$STOCK_PID" ] || ! ps -p "$STOCK_PID" >/dev/null 2>&1; then
            echo "[$(date)] 🍎 啟動股票 Runner..." >> "$LOG_DIR/unified.log"
            $PYTHON_EXEC "$UNIFIED_DIR/scripts/runners/stock_runner.py" >> "$LOG_DIR/stocks.log" 2>&1
            record_crash "stock" $? 0
            sleep 10
        else
            sleep 60  # Stock check every minute during market hours
        fi
    else
        sleep 300
    fi
done
) &

# ======================== Layer 7: Health Check Monitor ========================
(
echo "[$(date)] 💓 啟動健康檢查監控器 (每 ${HEALTH_CHECK_INTERVAL}s 檢測一次)" >> "$LOG_DIR/unified.log"

while true; do
    now=$(date +%s)
    
    STOCK_PIDS=$(pgrep -f "stock_runner.py" 2>/dev/null | wc -l)
    STREAMLIT_PIDS=$(pgrep -f "streamlit" 2>/dev/null | wc -l)
    
    # Check if log file is being updated (stale detection)
    LAST_MOD=$(stat -f %m "$LOG_DIR/unified.log" 2>/dev/null || echo 0)
    STALE_SECONDS=$((now - LAST_MOD))
    
    # Check disk space
    DISK_USAGE=$(df -h "$LOG_DIR" | awk 'NR==2 {print $5}' | sed 's/%//')
    
    # Status report
    STATUS=""
    H=$(date +%H)
    MM=$(date +%M)
    if [ "$H" -ge 8 ] && [ "$H" -lt 14 ]; then
        if [ "$STOCK_PIDS" -eq 0 ]; then
            STATUS="${STATUS}[❌股票]"
        else
            STATUS="${STATUS}[✅股票]"
        fi
    fi
    
    if [ "$STREAMLIT_PIDS" -lt 2 ]; then
        STATUS="${STATUS}[❌Dashboard]"
    else
        STATUS="${STATUS}[✅Dashboard]"
    fi
    
    if [ "$STALE_SECONDS" -gt 300 ]; then
        STATUS="${STATUS}[⚠️日誌停滯${STALE_SECONDS}s]"
    fi
    if [ "$DISK_USAGE" -gt 90 ]; then
        STATUS="${STATUS}[🚨磁碟${DISK_USAGE}%]"
        # Emergency cleanup
        find "$LOG_DIR" -name "*.log" -size +100M -delete 2>/dev/null
    fi

    # --- Maintenance: Auto-Archive (After sessions) ---
    if { [ "$H" -eq 13 ] && [ "$MM" -ge 45 ]; } || { [ "$H" -eq 5 ] && [ "$MM" -le 15 ]; }; then
        if [ ! -f "/tmp/archive.lock" ]; then
            echo "[$(date)] 📦 執行每日自動歸檔 (Maintenance Window)..." >> "$LOG_DIR/unified.log"
            $PYTHON_EXEC "$UNIFIED_DIR/scripts/maintenance/archive_daily_data.py" >> "$LOG_DIR/maintenance.log" 2>&1
            touch "/tmp/archive.lock"
            (sleep 3600 && rm -f "/tmp/archive.lock") &
        fi
    fi

    # --- Pre-Market: Sync Watchlist (08:30) ---
    if [ "$H" -eq 8 ] && [ "$MM" -ge 30 ] && [ "$MM" -lt 45 ]; then
        if [ ! -f "/tmp/sync.lock" ]; then
            echo "[$(date)] 🌐 執行開盤前名單同步 (Pre-Market)..." >> "$LOG_DIR/unified.log"
            $PYTHON_EXEC "$UNIFIED_DIR/scripts/maintenance/sync_watchlist_daily.py" >> "$LOG_DIR/maintenance.log" 2>&1
            touch "/tmp/sync.lock"
            (sleep 1800 && rm -f "/tmp/sync.lock") &
        fi
    fi

    # Heartbeat (every 30s)
    # Log status every 5 minutes (not every 30s)
    if [ $((now % 300)) -lt $HEALTH_CHECK_INTERVAL ]; then
        echo "[$(date)] 💓 狀態: $STATUS" >> "$LOG_DIR/unified.log"
    fi
    
    sleep $HEALTH_CHECK_INTERVAL
done
) &

# Wait for all background processes
wait
