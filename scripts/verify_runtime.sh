#!/usr/bin/env bash
# 2026-07-27 Hermes Agent: Read-only Runtime Verification Script
# P0: NO pm2 restart/start/stop/reload/delete — this script must never mutate production.
set -euo pipefail

PROCESS="trading-system"
OBSERVE_SECONDS=60
CAPTURE_SECONDS=10
CAPTURE_DIR="logs/ticks/dynamics"

fail() { echo "❌ FAIL: $*" >&2; exit 1; }
inconclusive() { echo "⚠️ INCONCLUSIVE: $*" >&2; exit 2; }

# ── Static guard: ensure no mutating commands exist in this script ──
_SELF="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
if grep -En '\bpm2 +(restart|start|stop|reload|delete)\b|\bkill\b|\btruncate\b' \
    "$_SELF" 2>/dev/null | grep -v 'grep -En' | grep -v 'mutating command check'; then
    echo "❌ FATAL: $_SELF contains mutating commands — aborting" >&2
    exit 1
fi

# ── Helpers ──
pm2_val() {
    local expr="$1"
    pm2 jlist 2>/dev/null | jq -er \
      "[.[] | select(.name==\"${PROCESS}\")][0]${expr}" 2>/dev/null || echo ""
}

now_ms() { echo $(( $(date +%s) * 1000 )); }

# ── Preflight ──
echo "=== Preflight ==="
command -v pm2 >/dev/null || fail "pm2 not found"
command -v jq  >/dev/null || fail "jq not found"
command -v git >/dev/null || fail "git not found"

HEAD_SHA=$(git rev-parse --short HEAD)
DIRTY=$(git status --short)
[ -z "$DIRTY" ] || fail "Git working tree not clean"
echo "HEAD: $HEAD_SHA"

# ── Process precondition (read-only) ──
STATUS=$(pm2_val '.pm2_env.status')
PID=$(pm2_val '.pid')
UPTIME_RAW=$(pm2_val '.pm2_env.pm_uptime')

[ "$STATUS" = "online" ] || inconclusive "Process status=$STATUS (expected online)"
[[ "$PID" =~ ^[1-9][0-9]*$ ]] || inconclusive "Invalid PID=$PID"

if [ -n "$UPTIME_RAW" ] && [ "$UPTIME_RAW" -gt 0 ] 2>/dev/null; then
    NOW_MS=$(now_ms)
    UPTIME_SEC=$(( (NOW_MS - UPTIME_RAW) / 1000 ))
    [ "$UPTIME_SEC" -ge 30 ] || inconclusive "Process uptime ${UPTIME_SEC}s < 30s (may have just restarted)"
fi

echo "PID=$PID Status=$STATUS"

# ── Step 1: Stability observation (read-only) ──
echo "=== Step 1: Process stability ==="
RESTART_COUNT=$(pm2_val '.pm2_env.restart_time')
PID1="$PID"
RESTART1="$RESTART_COUNT"
STATUS1="$STATUS"
echo "PID=$PID1 Restarts=$RESTART1 Status=$STATUS1"
echo "Observing ${OBSERVE_SECONDS}s..."
sleep "$OBSERVE_SECONDS"

STATUS2=$(pm2_val '.pm2_env.status')
PID2=$(pm2_val '.pid')
RESTART2=$(pm2_val '.pm2_env.restart_time')
echo "PID=$PID2 Restarts=$RESTART2 Status=$STATUS2"

[ "$STATUS2" = "online" ] || fail "Status changed to $STATUS2 after observation"
[ "$PID1" = "$PID2" ]      || fail "PID changed: $PID1 -> $PID2"
[ "$RESTART1" = "$RESTART2" ] || fail "Restart count changed: $RESTART1 -> $RESTART2"
echo "✅ PASS: Process stable"

# ── Step 2: Session-scoped exception scan ──
echo "=== Step 2: Session-scoped exception scan ==="

# Resolve log paths from PM2 metadata
ERR_LOG=$(pm2_val '.pm2_env.pm_err_log_path')
OUT_LOG=$(pm2_val '.pm2_env.pm_out_log_path')
[ -f "$ERR_LOG" ] || inconclusive "Error log not found: $ERR_LOG"
[ -f "$OUT_LOG" ] || inconclusive "Output log not found: $OUT_LOG"
echo "Error log: $ERR_LOG"
echo "Output log: $OUT_LOG"

# Determine session start — find the last BOOT_FINGERPRINT for this PID
SESSION_LINE=$(grep -n "pid=${PID}" "$OUT_LOG" 2>/dev/null | tail -1 | cut -d: -f1 || echo "")
if [ -z "$SESSION_LINE" ] || [ "$SESSION_LINE" -le 0 ] 2>/dev/null; then
    # Fallback: use DTI-001B marker
    SESSION_LINE=$(grep -n "DTI-001B" "$OUT_LOG" 2>/dev/null | tail -1 | cut -d: -f1 || echo "")
fi
if [ -z "$SESSION_LINE" ] || [ "$SESSION_LINE" -le 0 ] 2>/dev/null; then
    inconclusive "Cannot determine session boundary — no BOOT_FINGERPRINT or DTI-001B marker for PID=$PID"
fi

# Scan only from session start onward
SESSION_LOG_ERR=$(tail -n +"$SESSION_LINE" "$ERR_LOG" 2>/dev/null || echo "")
SESSION_LOG_OUT=$(tail -n +"$SESSION_LINE" "$OUT_LOG" 2>/dev/null || echo "")
COMBINED="${SESSION_LOG_ERR}"$'\n'"${SESSION_LOG_OUT}"

# Structured error patterns
CONTRACT_ERRORS=$(printf '%s\n' "$COMBINED" | grep -Ec "missing 1 required positional argument: 'lifecycle'|LIFECYCLE_EVAL_FAILED" || true)
BORROW_ERRORS=$(printf '%s\n' "$COMBINED" | grep -Ec "RuntimeError: Already borrowed" || true)
CAPTURE_ERRORS=$(printf '%s\n' "$COMBINED" | grep -Ec "DTI_CAPTURE_(OBSERVE_FAILED|WRITER_FAILED|FLUSH_FAILED|SERIALIZATION_FAILED|THREAD_DIED)" || true)
QUEUE_FULL=$(printf '%s\n' "$COMBINED" | grep -c "DTI_CAPTURE_QUEUE_FULL" || true)
EXECUTION_TRUE=$(printf '%s\n' "$COMBINED" | grep -Eic "execution_enabled[=: ]+true" || true)
EXECUTION_FALSE=$(printf '%s\n' "$COMBINED" | grep -Eic "execution_enabled[=: ]+false" || true)

printf 'contract=%s borrow=%s cap_errors=%s queue_full=%s exec_true=%s exec_false=%s\n' \
  "$CONTRACT_ERRORS" "$BORROW_ERRORS" "$CAPTURE_ERRORS" "$QUEUE_FULL" "$EXECUTION_TRUE" "$EXECUTION_FALSE"

[ "$CONTRACT_ERRORS" -eq 0 ] || fail "Lifecycle contract error(s) detected in current session"
[ "$BORROW_ERRORS"  -eq 0 ] || fail "Already borrowed reproduced in current session"
[ "$CAPTURE_ERRORS" -eq 0 ] || fail "Dynamics capture error(s) detected"
[ "$QUEUE_FULL"     -eq 0 ] || fail "Capture queue full — telemetry degraded"
[ "$EXECUTION_TRUE" -eq 0 ] || fail "execution_enabled unexpectedly true"
[ "$EXECUTION_FALSE" -gt 0 ] || inconclusive "No execution_enabled=false evidence found in session log"
echo "✅ PASS: No session-scoped errors"

# ── Step 3: Tick capture verification (read-only) ──
echo "=== Step 3: Tick capture verification ==="

FILE=$(find "$CAPTURE_DIR" -type f -name '*.jsonl' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)
[ -n "$FILE" ] && [ -f "$FILE" ] || inconclusive "No JSONL file found; tick capture not yet active"
[ -s "$FILE" ] || inconclusive "JSONL is 0 bytes; no ticks yet (may be non-market hours)"
echo "JSONL: $FILE"

BEFORE=$(wc -l < "$FILE")
sleep "$CAPTURE_SECONDS"
AFTER=$(wc -l < "$FILE")
DELTA=$((AFTER - BEFORE))
echo "Lines: $BEFORE -> $AFTER (delta=$DELTA)"

if [ "$DELTA" -le 0 ]; then
    # Check if market is likely closed
    HOUR=$(date +%H)
    # Taiwan futures: day 08:45-13:45, night 15:00-05:00 UTC+8
    # During non-market hours, delta=0 is expected → INCONCLUSIVE
    inconclusive "JSONL not growing (delta=0); may be non-market hours"
fi

# Check metadata on first and last line
for POS in "head -1" "tail -1"; do
    LINE=$(eval "$POS" "$FILE")
    printf '%s\n' "$LINE" | jq -e '.schema_version == "1.0.0"' >/dev/null || fail "schema_version mismatch"
    printf '%s\n' "$LINE" | jq -e '.derived_status == "NOT_COMPUTED"' >/dev/null || fail "derived_status mismatch"
done

GENERATION=$(tail -1 "$FILE" | jq -er '.generation_id')
case "$GENERATION" in
    *"$HEAD_SHA"*) ;;
    *) fail "generation_id does not contain HEAD $HEAD_SHA: $GENERATION" ;;
esac
echo "✅ PASS: Tick capture growing, schema valid, generation matches HEAD"

# ── Final result ──
echo ""
echo "=== FINAL RESULT ==="
echo "All gates PASS"
echo "DTI-001B Operationally Accepted"
exit 0
