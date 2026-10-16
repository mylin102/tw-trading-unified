# Phase 1 Release Candidate — Candidate Manifest (FREEZE → RE-FREEZE)

**日期**: 2026-08-09（re-freeze: 2026-08-09 Phase-2 recovery）
**狀態**: FROZEN — 未 deploy / 未 restart / 未讀 broker / 未跑 replay
**Frozen candidate SHA (RE-FREEZE)**: `ba9ccaf4ebd94fc92ff81ddc0e4b15c0b377b097`
**（原 freeze）**: `39eb943a63cea4ba7e3d2239829329c06ca740e9`
**Re-freeze 理由**: Phase-2 驗證發現原 freeze HEAD 已移（Phase-1b test-only
fix commit ba9ccaf4）→ 依 recovery 指令將 candidate 重新釘在含 test fix 的
code HEAD。Code HEAD = ba9ccaf4；其上僅有 docs-only commit
（`e6369072` Phase-2 manifest、`268dcbc5` 原 manifest）— 非 code。
**Closure base**: `fca6b70a3add245624cfdc93fbd13a4f8cf2d18c`（replay engine
commit — live wiring 前）
**Closure commits**: 37（`git rev-list --count fca6b70a..ba9ccaf4`）

## 1) Closure commit 清單（Steps 1-9 + corrective + closures）

| Step | 內容 | test commit | fix commit |
|---|---|---|---|
| Step 1 | monitor startup → certificate-required transition | 2282b3f7 | 00863f2d |
| Step 2 | _place_safety_stop gate | 599e069d | 1cb4e291 |
| Step 3 | _cancel_safety_stop gate | 10c3dd45 | bb04cc5c |
| Step 4 | _execute_trade direct-path gate | 2dc8b410 | 4b78563f |
| Step-4 corrective | ctx=None fail-closed (NO_LIVE_CERTIFICATION) | 00bbaefa | 139eedbd |
| Step 5 | reconnect atomic handoff | 56ba5479 | d5a80284 |
| Step-5 corrective | recert 失敗 return False（兩分支） | b82eac98 | 2dfbf0e7 |
| Step 6 | execution-context persistence | 9fb85b7f | 90b8d904 |
| Step 7 | logout/session invalidation wiring | 68198e7a/39f8b151 | cdc51656 |
| Step 8 | exhaustive adapter route gate | 55cc1812/40034ab4/cc1f5998 | 2b263d9c/aafce7bc |
| Step 9 | emergency quarantine gate | b9ef4ba8 | a96613ba |
| Exit failure-side | cancel 失敗 → 不靜默 place | 8fd2ac47 | 60f9b604 |
| Orphan reconciliation | SAFETY_STOP_RECONCILE intent | 6d0cc304 | 98f05287 |
| Release identity | core/release_identity + startup wiring | f0f8a95a/f1064110 | 94bd3c47 |
| Margin floor | config key + trusted source 驗證 | b6f82c5e | 39eb943a |
| Phase-1b fix | P0-adapter fixtures LIVE_READY ctx + startup LRC_RELEASE_SHA env | — | ba9ccaf4 |

## 2) Changed-file scope（closure range, fca6b70a..ba9ccaf4）

```
config/futures.yaml                                    # margin key (+100000.0)
core/execution_context_state.py   [NEW]                # Step 6
core/release_identity.py          [NEW]                # release identity
main.py                                                # Step 5/6/7 wiring
strategies/futures/monitor.py                          # Steps 1-9 wiring
strategies/futures/squeeze_futures/data/shioaji_client.py  # Step 8 gate
tests/core/test_adapter_route_gate.py            [NEW]
tests/core/test_execution_context_state.py       [NEW]
tests/core/test_logout_invalidation_wiring.py    [NEW]
tests/core/test_margin_floor_wiring.py           [NEW]
tests/core/test_release_identity_wiring.py       [NEW]
tests/core/test_live_route_monitor_integration.py
tests/core/test_live_route_wiring_behavioural.py
```
（13 files；replay 線 research 檔不在 closure range 內）

## 3) PM2 / config untouched check

- closure range 內 **無** `ecosystem.config.js` / `.env` / `pm2*` 變更 ✓
- 無 deploy / restart 執行（本 freeze 未發任何 PM2 命令）
- config 唯一變更 = `config/futures.yaml` margin key（關閉所需）

## 4) git status（working tree）

- closure commits 全部 committed；工作樹另有**非本 closure 的既有 dirty**：
  `data/tmf_full_2026.csv`、`scripts/model_c_validation.py`、
  `strategies/futures/mts_ledger_authority.py`、
  `tests/strategies/test_mts_ledger_authority.py` +
  untracked `data/telemetry/renko_bricks/`、shadow-soak generations —
  未 commit、未改動（manifest 記錄）

## 5) 測試狀態

**Full tests/core regression: NOT_COMPLETED**（如 freeze 指示，不宣稱 PASS）：
- 啟動完整 `pytest tests/core/ -q`（背景, 本地 pid 95020 / remote 36604）
- **43 分鐘（2578s）未完成** — 超出 stated 8-12min timeout → 依指示安全停止
  （本地 process.kill + remote kill 36604/36603；無 summary 輸出，tail 未 flush）
- 停止時另有 sibling 的 pytest（38980/38981, `-p no:cacheprovider`）—
  非本 freeze 產物，未觸碰
- **可能 hung test**（疑似等待/真 broker/網路呼叫）→ 列 limitation，
  需 per-file 分檔 + timeout 重跑定位

**Focused suites（各 Step 已驗, 全 GREEN）**：
- Step-1 startup：5/5；mode-transition+cert core：103 passed
- Step-2/3/4 + corrective：safety-stop/execute_trade 3 focused 全 PASSED
- Step-5 reconnect：6/6；Step-6 persistence：8/8
- Step-7 logout：5/5；Step-8 adapter：8/8 + exhaustive/inventory/manager-gate
- Step-9 emergency：5/5；exit failure-side：4/4；orphan：16/16 相關
- release identity：10/10；margin floor：12/12

## 6) Limitations（frozen）

1. **Deployment-only blocker（必須解決才可 deploy）**: PM2 必須在
   ecosystem.config.js（app: trading-system）env 注入 **literal
   `LRC_RELEASE_SHA` = deployed release HEAD** — 目前 ecosystem.config.js
   0 處注入 → LIVE startup 會 fail-closed（RELEASE_IDENTITY_ENV_MISSING）。
   **本次 re-freeze 未修改 ecosystem.config.js / PM2 env**（依指令）。
2. Full tests/core 逐檔 matrix：49 PASS / 2 FAIL（out-of-scope 既有 RED:
   test_background_snapshot_writer、test_global_callback_adapter）/ 1 TIMEOUT
   （out-of-scope 既有: test_market_data_runtime EXIT=124 — summary 後
   non-daemon thread hang，需獨立 bounded fix）
3. 未 deploy（依 freeze 指示）；PM2 上仍是先前 release（Release B 4adda0be）
4. dashboard 端 streamlit 讀取路徑未驗證
5. working tree 另有非 closure 的 sibling dirty（446 entries —
   data/telemetry、ledger 等）— 未 commit、未改動
6. sibling agent 同時在跑自身 tests/ 流程 — 工作樹可能再變動
