#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIFIED_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$UNIFIED_DIR/logs"
LOG_FILE="$LOG_DIR/dashboard.log"
PORT="${DASHBOARD_PORT:-8500}"
APP_PATH="${DASHBOARD_APP:-ui/dashboard.py}"
PYTHON_EXEC="${PYTHON_EXEC:-python3}"

mkdir -p "$LOG_DIR"
cd "$UNIFIED_DIR"

echo "[$(date)] 🔎 Dashboard preflight starting..."
"$PYTHON_EXEC" - <<'PY'
from core.dashboard_data import (
    build_stock_orders_from_trades,
    merge_indicator_frames,
    resolve_preferred_or_latest_file,
    resolve_stock_orders_file,
)
from core.dashboard_positions import (
    latest_indicator_close,
    option_order_matches_open_position,
)

assert callable(build_stock_orders_from_trades)
assert callable(resolve_stock_orders_file)
assert callable(latest_indicator_close)
assert callable(option_order_matches_open_position)
print("dashboard-preflight-ok")
PY

OLD_PID="$(lsof -ti tcp:$PORT | head -n 1 || true)"
if [ -n "$OLD_PID" ]; then
    echo "[$(date)] 🛑 Stopping old dashboard PID=$OLD_PID on :$PORT"
    kill "$OLD_PID"
    sleep 2
fi

echo "[$(date)] 🚀 Starting dashboard on :$PORT"
nohup "$PYTHON_EXEC" -m streamlit run "$APP_PATH" \
    --server.port "$PORT" \
    --server.address 127.0.0.1 \
    --server.headless true \
    >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "[$(date)] 📊 Dashboard PID=$NEW_PID launched"

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "http://127.0.0.1:$PORT" >/dev/null 2>&1; then
        echo "[$(date)] ✅ Dashboard healthy on :$PORT"
        exit 0
    fi
    sleep 2
done

echo "[$(date)] ❌ Dashboard failed health check on :$PORT"
tail -n 40 "$LOG_FILE" || true
exit 1
