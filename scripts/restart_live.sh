#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
PY="${TRADING_PYTHON_BIN:-/Users/myllin_mini/Documents/mylin102/tw-trading-unified-git/.venv/bin/python3}"
ENV_FILE="${TRADING_ENV_FILE:-/Users/myllin_mini/Documents/mylin102/tw-trading-unified-git/.env}"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: tracked changes must be committed before restart" >&2
  exit 1
fi

if ! "$PY" scripts/deployment/re_freeze.py --verify >/dev/null 2>&1; then
  "$PY" scripts/deployment/re_freeze.py
  git add PHASE1_FINAL_FREEZE.md
  AGENT_NAME="${AGENT_NAME:-human}" git commit -m "chore(deploy): re-freeze before live restart"
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: credentials env file not found: $ENV_FILE" >&2
  exit 1
fi
set -a
. "$ENV_FILE"
set +a
export LRC_RELEASE_SHA="$(git rev-parse HEAD)"
export TRADING_RUNTIME_DIR="/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime"
# [reconcile] mark stale pending MTS orders BROKER_NOT_FOUND (broker query truth;
# cancelled_at irrelevant)
# before restart so the entry guard is not blocked by phantom in-flight orders.
"$PY" scripts/reconcile_pending_orders.py || echo "WARN: reconcile skipped"
pm2 restart trading-system --update-env
sleep 25
"$PY" - <<'PY'
import json, subprocess, sys
items = json.loads(subprocess.check_output(["pm2", "jlist"], text=True))
app = next(x for x in items if x.get("name") == "trading-system")
status = app.get("pm2_env", {}).get("status")
print(f"trading-system status={status}")
sys.exit(0 if status == "online" else 1)
PY
