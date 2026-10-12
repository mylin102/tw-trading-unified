#!/usr/bin/env bash
# 2026-10-12 Codex: one entry point; defaults to read-only planning.
set -euo pipefail
DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${TRADING_PYTHON_BIN:?Set TRADING_PYTHON_BIN to the approved dependency interpreter}"
exec /usr/sbin/taskpolicy -c background "$TRADING_PYTHON_BIN" \
  "$DEPLOY_ROOT/scripts/deployment/deploy_mini.py" "$@"
