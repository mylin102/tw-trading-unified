# Phase 2 Production Package Verification — Deployment Manifest

**日期**: 2026-08-09
**Frozen candidate**: `39eb943a63cea4ba7e3d2239829329c06ca740e9`
**Verification scope**: release package/tree ONLY（未 deploy / 未 restart /
未讀 broker/runtime / 未 place/cancel/modify / 未 run replay）
**Overall verdict**: **NO-GO**（2 項 FAIL — strict contract）

## Strict PASS/FAIL/NOT_ASSESSED table

| # | Item | Result | Evidence |
|---|---|---|---|
| 1 | Exact HEAD == frozen candidate | **FAIL** | HEAD = `ba9ccaf4ebd94fc92ff81ddc0e4b15c0b377b097` ≠ `39eb943a…`。Delta `39eb943a..HEAD` = 3 files：`PHASE1_RC_CANDIDATE.md`（docs）+ `tests/core/test_live_adapter_order_p0.py` + `tests/core/test_live_route_monitor_integration.py`（Phase-1b test-only fix）— **production code 零差異**（benign delta，但仍非 exact HEAD） |
| 2 | Clean tracked closure files | **PASS** | `git status --porcelain` on 13 closure files = **empty** ✓ |
| 3 | LRC_RELEASE_SHA binding | **FAIL** | code path 存在且 tested（`core/release_identity.py` + monitor startup 接線），但 **PM2 ecosystem.config.js 0 處注入 LRC_RELEASE_SHA** → LIVE startup 將 fail-closed（RELEASE_IDENTITY_ENV_MISSING）→ LIVE_READY 不可達。部署時必須補 env（本驗證未改動 ecosystem） |
| 4 | TRADING_RUNTIME_DIR | **PASS** | ecosystem.config.js env 注入（line 33/55，both apps）；`core/runtime_paths.runtime_root()` 讀取 authority 一致 ✓ |
| 5 | futures.yaml TMF margin key/source | **PASS** | line 150 `live_required_margin_per_pair: 100000.0`；parse 驗證 floor=100000.0（margin closure 直跑）✓ |
| 6 | Script/core importability | **PASS** | `core.execution_context_state / release_identity / exit_intent / live_route_certificate / mode_transition` + `strategies.futures.monitor` + `main` 全 import ok ✓ |
| 7 | Candidate manifest hashes | **PASS** | `PHASE1_RC_CANDIDATE.md` committed `268dcbc5`，含 frozen SHA `39eb943a` ×2 ✓ |
| 8 | PM2 deploy/restart / broker / runtime / replay | **NOT_ASSESSED** | 依 contract 禁止執行（未動） |

## Deployment readiness notes

1. **Release tree**：HEAD = `ba9ccaf4` = frozen closure + Phase-1b test-only fix（2 test 檔；production 不變）。若接受 test fix 進 candidate → **re-freeze at `ba9ccaf4`** 即可消 item-1 FAIL。
2. **LRC_RELEASE_SHA**：deploy 前必須在 ecosystem.config.js（app: trading-system）env 加 `LRC_RELEASE_SHA: "<deployed HEAD>"` — 否則 release-identity gate 恆 quarantine（fail-closed 安全，但 LIVE 不可用）。此為本次驗證唯一真正的 blocking gap。
3. **Known pre-existing（out-of-scope，不 block 但須知悉）**：
   - `test_market_data_runtime.py` EXIT=124（summary 後 non-daemon thread hang — 需獨立 bounded fix）
   - `test_background_snapshot_writer.py` / `test_global_callback_adapter.py` 各 1 個既有 RED
4. Working tree 另有 sibling 的未提交 dirty（data/telemetry 等）— 非 closure 檔案，不影響上述判定。


## Portable release-worktree PM2 config (ecosystem.config.js)

The release candidate runs from an ISOLATED worktree with NO tracked
.venv and must never receive PM2 log/pid files. The ecosystem config
enforces:

1. **TRADING_PYTHON_BIN** — REQUIRED in production (NODE_ENV=production
   at `pm2 start` time); the config throws (fail-closed) rather than
   guessing an interpreter from an untracked candidate-tree venv.
   Dev/paper may use the shared dependency venv fallback.
2. **Runtime log dir** — trading/dashboard/stock error/out/combined/pid
   files live under TRADING_RUNTIME_DIR/logs (or TRADING_LOG_DIR) —
   NEVER inside the release source tree.
3. **Pre-start validation** — the runtime log dir must exist and be
   writable or the config fails BEFORE PM2 starts any app.
4. **No secrets** — failure messages name variables only; env values
   are never echoed.
