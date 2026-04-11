#!/bin/bash
# Night session monitoring — checks every 5 minutes
# Usage: bash scripts/monitor_night_session.sh

LOG="/Users/mylin/Documents/mylin102/tw-trading-unified/logs/unified.log"
ALERT_LOG="/Users/mylin/Documents/mylin102/tw-trading-unified/logs/night_monitor.log"

echo "=== Night Session Monitor $(date '+%Y-%m-%d %H:%M') ===" >> "$ALERT_LOG"

# 1. Check process health
PID=$(pgrep -f "main.py" | head -1)
if [ -z "$PID" ]; then
    echo "🚨 main.py NOT RUNNING!" >> "$ALERT_LOG"
else
    echo "✅ main.py PID=$PID" >> "$ALERT_LOG"
fi

# 2. Check for new bars in last 20 minutes
NEW_BARS=$(tail -500 "$LOG" | grep "New Bar.*1[6-9]:\|New Bar.*2[0-3]:\|New Bar.*0[0-4]:" | tail -5)
if [ -n "$NEW_BARS" ]; then
    echo "📊 Recent bars:" >> "$ALERT_LOG"
    echo "$NEW_BARS" >> "$ALERT_LOG"
else
    echo "⚠️ No new bars in last 20 min" >> "$ALERT_LOG"
fi

# 3. Check for trades
TRADES=$(tail -1000 "$LOG" | grep -E "🟢 BUY|🔴 SELL|⚪ EXIT" | tail -5)
if [ -n "$TRADES" ]; then
    echo "📈 Recent trades:" >> "$ALERT_LOG"
    echo "$TRADES" >> "$ALERT_LOG"
else
    echo "📭 No recent trades" >> "$ALERT_LOG"
fi

# 4. Check for anomalies
ANOMALIES=$(tail -1000 "$LOG" | grep -E "SL=[0-9]{5,}|friction|stagnation|DATA WARNING|DATA STAGNATION" | tail -5)
if [ -n "$ANOMALIES" ]; then
    echo "🔍 Anomalies:" >> "$ALERT_LOG"
    echo "$ANOMALIES" >> "$ALERT_LOG"
fi

echo "---" >> "$ALERT_LOG"

# Print to stdout too
tail -20 "$ALERT_LOG"
