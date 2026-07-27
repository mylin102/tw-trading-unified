#!/usr/bin/env bash
# 2026-07-27 Hermes Agent: Controlled Restart & Verification Workflow
# MUTATING — explicitly restart production process with pre/post snapshots.
set -euo pipefail

PROCESS="trading-system"
VERIFY_SCRIPT="scripts/verify_runtime.sh"
SNAPSHOT_DIR="logs/runtime_verification"
TS=$(date -u +%Y%m%dT%H%M%SZ)

echo "⚠️  WARNING: This script will restart production process: $PROCESS"
echo "    Continue? (y/N): "
read -r CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || { echo "Aborted."; exit 0; }

mkdir -p "$SNAPSHOT_DIR"

# ── Pre-restart snapshot ──
echo "=== Pre-restart snapshot ==="

pm2 jlist > "$SNAPSHOT_DIR/${TS}_pre_pm2.json"
git rev-parse HEAD > "$SNAPSHOT_DIR/${TS}_pre_git_head.txt"
git status --short > "$SNAPSHOT_DIR/${TS}_pre_git_status.txt" 2>/dev/null || true

# Capture snapshot
PRE_FILE=$(find logs/ticks/dynamics -type f -name '*.jsonl' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || echo "")
if [ -n "$PRE_FILE" ] && [ -f "$PRE_FILE" ]; then
    wc -l < "$PRE_FILE" > "$SNAPSHOT_DIR/${TS}_pre_capture_lines.txt"
    stat -f '%Sm' "$PRE_FILE" > "$SNAPSHOT_DIR/${TS}_pre_capture_mtime.txt" 2>/dev/null || true
    echo "$PRE_FILE" > "$SNAPSHOT_DIR/${TS}_pre_capture_path.txt"
fi

echo "  Snapshot saved to $SNAPSHOT_DIR/${TS}_pre_*"

# ── Controlled restart ──
echo "=== Restarting $PROCESS ==="
pm2 restart "$PROCESS"
sleep 5

# ── Post-restart snapshot ──
echo "=== Post-restart snapshot ==="

pm2 jlist > "$SNAPSHOT_DIR/${TS}_post_pm2.json"
git rev-parse HEAD > "$SNAPSHOT_DIR/${TS}_post_git_head.txt"
CURRENT_GEN=$(find logs/ticks/dynamics -type f -name '*.jsonl' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || echo "")
if [ -n "$CURRENT_GEN" ] && [ -f "$CURRENT_GEN" ]; then
    echo "$CURRENT_GEN" > "$SNAPSHOT_DIR/${TS}_post_capture_path.txt"
fi

echo "  Snapshot saved to $SNAPSHOT_DIR/${TS}_post_*"

# ── Verification ──
echo "=== Running verify_runtime.sh ==="
bash "$VERIFY_SCRIPT"
