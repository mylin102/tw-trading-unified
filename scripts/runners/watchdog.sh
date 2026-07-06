#!/bin/bash
# Process watchdog — restarts main.py if it crashes (handles Shioaji C++ crashes)
# Usage: bash scripts/watchdog.sh
LOG="/Users/mylin/Documents/mylin102/tw-trading-unified/logs/watchdog.log"
MAX_RESTARTS=20
RESTARTS=0
BACKOFF=3  # Initial backoff seconds

echo "🔒 Watchdog started $(date)" >> "$LOG"

while [ $RESTARTS -lt $MAX_RESTARTS ]; do
    # Check if main.py is running
    PID=$(pgrep -f "python3 main.py" | head -1)
    
    if [ -z "$PID" ]; then
        RESTARTS=$((RESTARTS + 1))
        echo "💀 main.py not found! Restarting ($RESTARTS/$MAX_RESTARTS) $(date)" >> "$LOG"
        
        # Exponential backoff: 3, 6, 12, 24, 30, 30... (cap at 30s)
        if [ $BACKOFF -lt 30 ]; then
            BACKOFF=$((BACKOFF * 2))
        fi
        echo "   ⏳ Backoff: ${BACKOFF}s" >> "$LOG"
        sleep $BACKOFF
        
        # Restart
        cd /Users/mylin/Documents/mylin102/tw-trading-unified
        nohup python3 main.py >> /Users/mylin/Documents/mylin102/tw-trading-unified/logs/unified.log 2>&1 &
        NEW_PID=$!
        echo "✅ Restarted: PID=$NEW_PID" >> "$LOG"
        
        # Wait for startup grace period
        sleep 15
        
        # Verify it actually started
        NEW_PID2=$(pgrep -f "python3 main.py" | head -1)
        if [ -z "$NEW_PID2" ]; then
            echo "🚨 Restart FAILED — main.py crashed on startup" >> "$LOG"
        else
            echo "✅ Verified running: PID=$NEW_PID2" >> "$LOG"
            # Reset backoff on successful start
            RESTARTS=0
            BACKOFF=3
        fi
    else
        # Process alive — check every 30 seconds
        sleep 30
    fi
done

echo "🚨 Max restarts ($MAX_RESTARTS) reached — giving up $(date)" >> "$LOG"
