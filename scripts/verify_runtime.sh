#!/usr/bin/env bash
# 2026-07-27 Gemini CLI: Production-grade Runtime Verification Script for PM2, Exceptions & DTI Tick Capture
set -euo pipefail

PROCESS="trading-system"
OBSERVE_SECONDS=60
CAPTURE_SECONDS=10
CAPTURE_DIR="logs/ticks/dynamics"

fail() {
    echo "❌ FAIL: $*" >&2
    exit 1
}

inconclusive() {
    echo "⚠️ INCONCLUSIVE: $*" >&2
    exit 2
}

pm2_value() {
    local expression="$1"
    pm2 jlist 2>/dev/null |
        jq -er "[.[] | select(.name==\"${PROCESS}\")][0]${expression}" 2>/dev/null
}

echo "=== Preflight ==="

command -v pm2 >/dev/null || fail "pm2 不存在"
command -v jq >/dev/null || fail "jq 不存在"
command -v git >/dev/null || fail "git 不存在"

HEAD_SHA=$(git rev-parse --short HEAD)
DIRTY=$(git status --short)

[ -z "$DIRTY" ] || fail "Git working tree 不乾淨"
echo "HEAD: $HEAD_SHA"

echo "=== Step 1: Controlled restart and process stability ==="

pm2 restart "$PROCESS" >/dev/null

sleep 5

STATUS1=$(pm2_value '.pm2_env.status')
PID1=$(pm2_value '.pid')
RESTART1=$(pm2_value '.pm2_env.restart_time')

[ "$STATUS1" = "online" ] || fail "重啟後 status=$STATUS1"
[[ "$PID1" =~ ^[1-9][0-9]*$ ]] || fail "無效 PID1=$PID1"

echo "PID1=$PID1 RestartCount1=$RESTART1 Status1=$STATUS1"
echo "觀測 ${OBSERVE_SECONDS} 秒..."
sleep "$OBSERVE_SECONDS"

STATUS2=$(pm2_value '.pm2_env.status')
PID2=$(pm2_value '.pid')
RESTART2=$(pm2_value '.pm2_env.restart_time')

echo "PID2=$PID2 RestartCount2=$RESTART2 Status2=$STATUS2"

[ "$STATUS2" = "online" ] || fail "觀測後 status=$STATUS2"
[ "$PID1" = "$PID2" ] || fail "PID changed: $PID1 -> $PID2"
[ "$RESTART1" = "$RESTART2" ] ||
    fail "restart count changed: $RESTART1 -> $RESTART2"

echo "✅ PASS: PM2 process stable"

echo "=== Step 2: Current-runtime exception scan ==="

LOGS=$(pm2 logs "$PROCESS" --lines 500 --nostream 2>/dev/null || true)

CONTRACT_ERRORS=$(
    printf '%s\n' "$LOGS" |
    grep -Ec \
    "missing 1 required positional argument: ['\"]lifecycle['\"]|LIFECYCLE_EVAL_FAILED" \
    || true
)

BORROW_ERRORS=$(
    printf '%s\n' "$LOGS" |
    grep -Ec "RuntimeError: Already borrowed|Already borrowed" \
    || true
)

WRITER_ERRORS=$(
    printf '%s\n' "$LOGS" |
    grep -Eic "dynamics.*writer.*(error|exception|failed)" \
    || true
)

EXECUTION_TRUE=$(
    printf '%s\n' "$LOGS" |
    grep -Eic "execution_enabled[=: ]+true" \
    || true
)

printf \
  'contract_errors=%s borrow_errors=%s writer_errors=%s execution_true=%s\n' \
  "$CONTRACT_ERRORS" "$BORROW_ERRORS" "$WRITER_ERRORS" "$EXECUTION_TRUE"

[ "$CONTRACT_ERRORS" -eq 0 ] ||
    fail "Lifecycle contract error detected"
[ "$BORROW_ERRORS" -eq 0 ] ||
    fail "Already borrowed reproduced"
[ "$WRITER_ERRORS" -eq 0 ] ||
    fail "Dynamics writer error detected"
[ "$EXECUTION_TRUE" -eq 0 ] ||
    fail "execution_enabled unexpectedly true"

printf '%s\n' "$LOGS" |
    grep -Eq "execution_enabled[=: ]+false" ||
    fail "未找到 execution_enabled=false 啟動證據"

echo "✅ PASS: No runtime contract/crash exception"

echo "=== Step 3: Tick capture verification ==="

FILE="$(
    find "$CAPTURE_DIR" -type f -name '*.jsonl' \
        -printf '%T@ %p\n' 2>/dev/null |
    sort -nr |
    head -1 |
    cut -d' ' -f2-
)"

[ -n "$FILE" ] || inconclusive "沒有 JSONL；無法完成 operational acceptance"
[ -f "$FILE" ] || fail "Latest JSONL path invalid: $FILE"
[ -s "$FILE" ] || fail "Latest JSONL is still 0 bytes: $FILE"

echo "Latest JSONL: $FILE"

BEFORE=$(wc -l < "$FILE")
sleep "$CAPTURE_SECONDS"
AFTER=$(wc -l < "$FILE")
DELTA=$((AFTER - BEFORE))

printf 'before=%s after=%s delta=%s\n' "$BEFORE" "$AFTER" "$DELTA"

[ "$DELTA" -gt 0 ] ||
    fail "JSONL 未在 ${CAPTURE_SECONDS} 秒內增長"

LAST_LINE=$(tail -n 1 "$FILE")

printf '%s\n' "$LAST_LINE" |
    jq -e '.schema_version == "1.0.0"' >/dev/null ||
    fail "schema_version mismatch"

printf '%s\n' "$LAST_LINE" |
    jq -e '.derived_status == "NOT_COMPUTED"' >/dev/null ||
    fail "derived_status mismatch"

GENERATION=$(
    printf '%s\n' "$LAST_LINE" |
    jq -er '.generation_id'
)

case "$GENERATION" in
    *"$HEAD_SHA"*) ;;
    *) fail "generation_id does not contain HEAD $HEAD_SHA: $GENERATION" ;;
esac

echo "✅ PASS: DTI tick capture is non-empty and growing"

echo "=== FINAL RESULT ==="
echo "INC-20260727-A: CLOSED"
echo "INC-20260727-B: NOT REPRODUCED DURING CONTROLLED OBSERVATION"
echo "DTI-001B: OPERATIONALLY ACCEPTED"
echo "Capture Mode: ENABLED"
echo "Execution Influence: HARD-LOCKED FALSE"
echo "Coverage Accumulation: STARTED"
