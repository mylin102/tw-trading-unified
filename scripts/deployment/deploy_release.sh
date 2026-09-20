#!/usr/bin/env bash
# Single-command, fail-closed LIVE deploy for the MTS futures app (Collapse 1).
#
#   bash scripts/deployment/deploy_release.sh [--dry-run] [--app NAME]
#
# One command, one approval: every stage below runs in order and the script
# aborts on the FIRST non-zero result.  There is no retry, no bypass flag and
# no "--force" — a failed gate means the deploy stops and the operator gets a
# typed [FAIL] line naming the stage and the rc.
#
# Stages
#   1 PREFLIGHT   repo/python/runtime/.env present, tracked tree clean, disk floor, app known
#   2 FREEZE      re_freeze --verify, else re-freeze + commit manifest; pin LRC_RELEASE_SHA=HEAD
#   3 IMPACT      contract_impact.py on (running sha -> HEAD); a FULL verdict needs a
#                 recorded verification of that sha (audit C7b call site)
#   4 GATE_PRE    check_deployment.py --phase pre_deploy   (CLI only, gate never bypassed)
#   5 STOP        pm2 stop <app> and confirm stopped
#   6 RECONCILE   scripts/reconcile_pending_orders.py (fail-closed)
#   7 START       pm2 start <app> with the pinned env (never --update-env)
#   8 GATE_POST   check_deployment.py --phase post_startup
#   9 VERIFY      verify_live_context.py: live_ready + live_order_allowed + pinned sha
#
# Safety properties (audited 2026-09-20):
#   * tools are NOT overridable — `git` and `pm2` resolve from PATH and every
#     safety threshold is a literal in this file; only the runtime's own env
#     (TRADING_RUNTIME_DIR / TRADING_ENV_FILE / TRADING_PYTHON_BIN) is read.
#   * `--dry-run` performs NO external action, writes NO file and never sources
#     the credentials file: it prints the plan only, so its rc=0 means "the plan
#     printed", never "the gates passed".
#   * every command whose result matters (gate, reconcile, env load, verify) is
#     checked; a failing `.env` or an unrecorded FULL verification aborts.
set -u -o pipefail

TOTAL_STAGES=9
CURRENT_STAGE="init"
STAGE_NO=0
DRY_RUN=0
APP="trading-system"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="${TRADING_RUNTIME_DIR:-/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime}"
PYTHON_BIN="${TRADING_PYTHON_BIN:-/Users/myllin_mini/Documents/mylin102/tw-trading-unified-release15/.venv/bin/python3}"
ENV_FILE="${TRADING_ENV_FILE:-/Users/myllin_mini/Documents/mylin102/tw-trading-unified-git/.env}"
GIT_BIN="git"
PM2_BIN="pm2"
MANIFEST="PHASE1_FINAL_FREEZE.md"
POSITION_STATE="$RUNTIME_DIR/logs/position_state.json"
PID_FILE="$RUNTIME_DIR/logs/pm2-trading-trading-20.pid"
MIN_FREE_GB=3
STOP_WAIT_S=2
START_WAIT_S=25

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --app) APP="$2"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) printf '[FAIL] unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '[DEPLOY] %s\n' "$*"; }
stage() {
  STAGE_NO=$((STAGE_NO + 1))
  CURRENT_STAGE="$1"
  printf '\n[STAGE %d/%d %s]\n' "$STAGE_NO" "$TOTAL_STAGES" "$1"
}
fail() {
  printf '\n[FAIL] stage=%s rc=%s %s\n' "$CURRENT_STAGE" "$1" "$2" >&2
  printf '[FAIL] deploy aborted at stage %s/%s — fix the cause; no retry was attempted\n' \
    "$STAGE_NO" "$TOTAL_STAGES" >&2
  exit "$1"
}
run() {  # run [--allow-fail] <cmd...>
  if [ "$DRY_RUN" = 1 ]; then log "DRY-RUN: $*"; return 0; fi
  "$@"
  local rc=$?
  [ $rc -eq 0 ] || fail "$rc" "command failed: $*"
  return 0
}

ARTIFACT_DIR="$RUNTIME_DIR/logs"
log "repo=$REPO_DIR runtime=$RUNTIME_DIR app=$APP dry_run=$DRY_RUN"

# ── 1 PREFLIGHT ───────────────────────────────────────────────────────────────
stage PREFLIGHT
[ -d "$REPO_DIR" ] || fail 2 "repo dir missing: $REPO_DIR"
[ -x "$PYTHON_BIN" ] || fail 2 "python bin missing/not executable: $PYTHON_BIN"
command -v "$GIT_BIN" >/dev/null 2>&1 || fail 2 "git not found on PATH"
command -v "$PM2_BIN" >/dev/null 2>&1 || fail 2 "pm2 not found on PATH"
[ -d "$RUNTIME_DIR" ] || fail 2 "runtime dir missing: $RUNTIME_DIR"
[ -f "$ENV_FILE" ] || fail 2 "credentials env file missing: $ENV_FILE"
cd "$REPO_DIR" || fail 2 "cannot cd $REPO_DIR"
if ! "$GIT_BIN" diff --quiet || ! "$GIT_BIN" diff --cached --quiet; then
  fail 2 "tracked changes must be committed before deploy"
fi
# disk floor: computed by verify_live_context.py (unit-tested pure function)
FREE_GB="$("$PYTHON_BIN" -B scripts/deployment/verify_live_context.py \
  --check-disk --min-free-gb "$MIN_FREE_GB" --runtime-dir "$RUNTIME_DIR" 2>/dev/null)" \
  || fail 2 "disk floor check failed (need >= ${MIN_FREE_GB}GB on $RUNTIME_DIR)"
[ -n "${FREE_GB:-}" ] || fail 2 "cannot read free space for $RUNTIME_DIR"
if [ "$DRY_RUN" = 0 ]; then
  [ -d "$ARTIFACT_DIR" ] || mkdir -p "$ARTIFACT_DIR"
  "$PM2_BIN" jlist > "$ARTIFACT_DIR/deploy_pm2_jlist.json" 2>/dev/null || fail 2 "pm2 jlist failed"
  "$PYTHON_BIN" -B -c "import json,sys; names=[x['name'] for x in json.load(open(sys.argv[1]))]; sys.exit(0 if sys.argv[2] in names else 3)" \
    "$ARTIFACT_DIR/deploy_pm2_jlist.json" "$APP" || fail 3 "pm2 app not registered: $APP"
fi
log "preflight OK (free ${FREE_GB}GB)"

# ── 2 FREEZE ─────────────────────────────────────────────────────────────────
stage FREEZE
if [ "$DRY_RUN" = 1 ]; then
  log "DRY-RUN: re_freeze.py --verify || re_freeze.py + commit manifest"
else
  if ! "$PYTHON_BIN" -B scripts/deployment/re_freeze.py --verify >/dev/null 2>&1; then
    log "freeze stale -> re-freeze"
    run "$PYTHON_BIN" -B scripts/deployment/re_freeze.py || fail 4 "re_freeze failed"
    run "$GIT_BIN" add "$MANIFEST" || fail 4 "git add manifest failed"
    AGENT_NAME="${AGENT_NAME:-human}" run "$GIT_BIN" commit -m "chore(deploy): re-freeze before live deploy" \
      || fail 4 "manifest commit failed"
  else
    log "freeze verify OK"
  fi
fi
EXPECTED_SHA="$( [ "$DRY_RUN" = 1 ] && echo DRYRUN || "$GIT_BIN" rev-parse HEAD )"
export LRC_RELEASE_SHA="$EXPECTED_SHA"
export TRADING_RUNTIME_DIR="$RUNTIME_DIR"
log "expected_sha=$LRC_RELEASE_SHA"

# ── 3 IMPACT ─────────────────────────────────────────────────────────────────
stage IMPACT
if [ "$DRY_RUN" = 1 ]; then
  log "DRY-RUN would run: contract_impact.py --base <running ${APP} sha> --head ${EXPECTED_SHA:-HEAD} --exit-code"
  log "DRY-RUN: the impact gate and the FULL-verification record are NOT checked in dry-run"
else
  if [ ! -f scripts/ci/contract_impact.py ]; then
    fail 4 "scripts/ci/contract_impact.py missing — cannot classify the deployed diff"
  fi
  RUNNING_SHA="$("$PM2_BIN" jlist > "$ARTIFACT_DIR/deploy_pm2_pre.json" 2>/dev/null; "$PYTHON_BIN" -B -c "import json,sys; items=json.load(open(sys.argv[1])); print(next((x['pm2_env'].get('LRC_RELEASE_SHA','') for x in items if x['name']==sys.argv[2]), ''))" "$ARTIFACT_DIR/deploy_pm2_pre.json" "$APP" 2>/dev/null)"
  IMPACT_BASE="${RUNNING_SHA:-}"
  if [ -z "$IMPACT_BASE" ]; then
    log "running sha unknown -> forcing FULL classification"
    IMPACT_BASE=""
  fi
  IMPACT_RC=0
  if [ -n "$IMPACT_BASE" ] && [ "$IMPACT_BASE" != "$EXPECTED_SHA" ]; then
    "$PYTHON_BIN" -B scripts/ci/contract_impact.py --repo "$REPO_DIR" --base "$IMPACT_BASE" \
        --head "$EXPECTED_SHA" --json --exit-code > "$ARTIFACT_DIR/deploy_impact.json" 2>"$ARTIFACT_DIR/deploy_impact.err"
    IMPACT_RC=$?
    log "impact decision rc=$IMPACT_RC (0=FAST 1=FULL 2=error)"
  else
    if [ -z "$IMPACT_BASE" ]; then
      printf '{"decision": "FULL", "reason": "running sha unknown"}\n' > "$ARTIFACT_DIR/deploy_impact.json"
      IMPACT_RC=1
    else
      printf '{"decision": "NO_CHANGE", "reason": "running sha == expected sha"}\n' > "$ARTIFACT_DIR/deploy_impact.json"
      IMPACT_RC=0
    fi
  fi
  [ "$IMPACT_RC" -ne 2 ] || fail 4 "contract-impact classification failed — see $ARTIFACT_DIR/deploy_impact.err"
  if [ "$IMPACT_RC" -eq 1 ]; then
    SCOPE_FILE="$ARTIFACT_DIR/verify_scope.json"
    [ -f "$SCOPE_FILE" ] || fail 4 "FULL verification required for this diff but $SCOPE_FILE is missing"
    "$PYTHON_BIN" -B -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('sha')==sys.argv[2] and d.get('decision')=='FULL' else 4)" \
      "$SCOPE_FILE" "$EXPECTED_SHA" || fail 4 "verify_scope.json does not record a FULL verification of $EXPECTED_SHA"
    log "FULL verification recorded for $EXPECTED_SHA"
  else
    log "impact decision FAST/NO_CHANGE — no FULL record required"
  fi
fi

# ── 4 GATE_PRE ───────────────────────────────────────────────────────────────
stage GATE_PRE
GATE_ARGS=(--release-dir "$REPO_DIR" --runtime-dir "$RUNTIME_DIR" --pid-file "$PID_FILE"
           --position-state "$POSITION_STATE" --monitor strategies/futures/monitor.py
           --expected-sha "$LRC_RELEASE_SHA" --manifest "$MANIFEST")
if [ "$DRY_RUN" = 1 ]; then
  log "DRY-RUN would run: $PYTHON_BIN -B scripts/deployment/check_deployment.py --phase pre_deploy ${GATE_ARGS[*]} --json"
  log "DRY-RUN: the pre-deploy gate is NOT executed in dry-run — nothing is verified"
else
  "$PYTHON_BIN" -B scripts/deployment/check_deployment.py --phase pre_deploy "${GATE_ARGS[@]}" \
      --json > "$ARTIFACT_DIR/deploy_gate_pre.json" 2>"$ARTIFACT_DIR/deploy_gate_pre.err"
  GATE_RC=$?
  [ $GATE_RC -eq 0 ] || fail 5 "gate pre_deploy NOT_READY (rc=$GATE_RC) — see $ARTIFACT_DIR/deploy_gate_pre.err"
  log "gate pre_deploy READY"
fi

# ── 4 STOP ───────────────────────────────────────────────────────────────────
stage STOP
run "$PM2_BIN" stop "$APP" || fail 6 "pm2 stop failed"
if [ "$DRY_RUN" = 0 ]; then
  sleep "$STOP_WAIT_S"
  "$PM2_BIN" jlist > "$ARTIFACT_DIR/deploy_pm2_after_stop.json" 2>/dev/null
  STATUS="$("$PYTHON_BIN" -B -c "import json,sys; items=json.load(open(sys.argv[1])); print(next((x['pm2_env']['status'] for x in items if x['name']==sys.argv[2]), 'missing'))" "$ARTIFACT_DIR/deploy_pm2_after_stop.json" "$APP")"
  [ "$STATUS" = "stopped" ] || fail 6 "app not stopped (status=$STATUS)"
  log "app stopped"
fi

# ── 5 RECONCILE ──────────────────────────────────────────────────────────────
stage RECONCILE
if [ -f scripts/reconcile_pending_orders.py ]; then
  run "$PYTHON_BIN" -B scripts/reconcile_pending_orders.py || fail 7 "reconcile_pending_orders failed (fail-closed)"
else
  fail 2 "scripts/reconcile_pending_orders.py missing — refusing to start with unreconciled orders"
fi

# ── 6 START ──────────────────────────────────────────────────────────────────
stage START
if [ "$DRY_RUN" = 1 ]; then
  log "DRY-RUN would load credentials from $ENV_FILE (not sourced in dry-run)"
else
  set -a
  if ! . "$ENV_FILE"; then
    set +a
    fail 8 "credentials env load failed: $ENV_FILE"
  fi
  set +a
fi
export LRC_RELEASE_SHA="$EXPECTED_SHA"
export TRADING_RUNTIME_DIR="$RUNTIME_DIR"
run "$PM2_BIN" start "$APP" || fail 8 "pm2 start failed"
if [ "$DRY_RUN" = 0 ]; then
  sleep "$START_WAIT_S"
  "$PM2_BIN" jlist > "$ARTIFACT_DIR/deploy_pm2_after_start.json" 2>/dev/null
  STATUS="$("$PYTHON_BIN" -B -c "import json,sys; items=json.load(open(sys.argv[1])); print(next((x['pm2_env']['status'] for x in items if x['name']==sys.argv[2]), 'missing'))" "$ARTIFACT_DIR/deploy_pm2_after_start.json" "$APP")"
  [ "$STATUS" = "online" ] || fail 8 "app not online (status=$STATUS)"
  log "app online"
fi

# ── 7 GATE_POST ──────────────────────────────────────────────────────────────
stage GATE_POST
SESSION_GEN="$("$PYTHON_BIN" -B -c "
import json,os,sys
p=os.environ.get('TRADING_RUNTIME_DIR','')+'/execution_context.json'
try: print(json.load(open(p)).get('session_id') or '')
except Exception: print('')
" 2>/dev/null)"
if [ "$DRY_RUN" = 1 ]; then
  log "DRY-RUN would run: check_deployment.py --phase post_startup ${GATE_ARGS[*]} --session-generation $SESSION_GEN --json"
else
  "$PYTHON_BIN" -B scripts/deployment/check_deployment.py --phase post_startup "${GATE_ARGS[@]}" \
      --session-generation "$SESSION_GEN" --json > "$ARTIFACT_DIR/deploy_gate_post.json" 2>"$ARTIFACT_DIR/deploy_gate_post.err"
  GATE_RC=$?
  [ $GATE_RC -eq 0 ] || fail 9 "gate post_startup NOT_READY (rc=$GATE_RC) — see $ARTIFACT_DIR/deploy_gate_post.err"
  log "gate post_startup READY"
fi

# ── 8 VERIFY ─────────────────────────────────────────────────────────────────
stage VERIFY
if [ "$DRY_RUN" = 1 ]; then
  log "DRY-RUN would run: verify_live_context.py --runtime-dir $RUNTIME_DIR --expected-sha $EXPECTED_SHA"
else
  run "$PYTHON_BIN" -B scripts/deployment/verify_live_context.py --runtime-dir "$RUNTIME_DIR" \
    --expected-sha "$EXPECTED_SHA" || fail 10 "live context verification failed"
fi

if [ "$DRY_RUN" = 1 ]; then
  printf '\n[DEPLOY] DRY-RUN plan only — NOTHING was verified (rc=0 means the plan printed, no gate ran)\n'
  exit 0
fi
printf '\n[DEPLOY OK] app=%s sha=%s stages=%d/%d\n' "$APP" "${EXPECTED_SHA:-unknown}" "$STAGE_NO" "$TOTAL_STAGES"
