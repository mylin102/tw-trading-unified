# Phase 1 Release Candidate — FINAL FREEZE + Preflight Packet

**日期**: 2026-08-09（Codex bounded step — final freeze）
**狀態**: FROZEN（freeze-first：所有 code/test commits 已完成後 freeze；
**此後禁改檔** — gate 對任何 post-freeze 變更 refuse）
**Frozen release tree HEAD**: `423e149eec41eaf8ea926667200d4fd5e5215e09`
（= serializer int-like exact-type fix commit；其後僅此 re-freeze docs commit）
（= gate closure fix commit；其後僅此 freeze-record docs commit）
**frozen_tree_hash（exclude-self tree identity — manifest/rollback docs
排除）**: `ed2f316240a6914dcbd6f5ab3cb0bd7805dd189397077a8dcbe4b14dfae19464`
（= `git ls-tree -r <commit> | grep -v manifest docs | sha256`；manifest-only
commits 不影響此 identity — 解除 freeze/manifest SHA cycle；任何 code
變更 → hash 變 → gate GUARD_MANIFEST_STALE 拒絕）
```
frozen_tree_hash: 44c2348ea3140113bcff7311823d5f706b20eeb61c65e2beae5a18ee7bf67474
```
**LRC_RELEASE_SHA（部署時）**: = deploy-time `git -C <release_dir>
rev-parse HEAD` 之 literal（= 此 freeze-record commit 的 SHA；deployed
tree 的 HEAD — gate 以 GUARD_HEAD_MISMATCH 強制一致）
**Closure**: Steps 1-9 + exit failure-side + orphan + release identity +
margin + Phase-1b fix + Deployment Safety Gate（7db0e0a1/63ea9e68，
AGY 驗收 44/44）

## Clean-tree 證據（freeze 時刻）
```
git status --porcelain -- <closure 11 files>  = (empty)
git diff --exit-code -- ecosystem.config.js   = clean（未修改）
```

## Preflight Packet（read-only — NOT_READY，無補值）

| Item | 現況 | 狀態 |
|---|---|---|
| source=live_broker | **缺**（position state 無 source/mode） | NOT_READY |
| positions=[] | **缺**（has_position=True, HOLDING_SPREAD；欄位全 None） | NOT_READY |
| open_orders=[] | 缺 | NOT_READY |
| captured_at ≤600s | 缺（_updated 2026-08-08T04:59 — stale） | NOT_READY |
| snapshot hash | 缺 | NOT_READY |
| session_id | 缺（ctx 檔不存在 — Release B 未寫） | NOT_READY |
| margin ≥ 220000 | 無唯讀來源（禁 broker read） | NOT_READY |
| execution_context | 缺檔 → gate 判定 quarantine-bootstrap（原子寫入能力已驗證） | PASS（bootstrap only, never LIVE） |

**Packet verdict: NOT_READY** — 部署前需由授權的 broker 唯讀快照提供
source=live_broker 的 flat 證明 + margin + session（本 freeze 不補值）。

## Deployment Gate dry/read-only 結果（production env）
```
[PASS] release_head / clean_tree / runtime_paths / single_process /
       quarantine_first_startup / ctx_atomic_health(bootstrap)
[FAIL] flat_snapshot:      GUARD_SNAPSHOT_SOURCE_AMBIGUOUS
[FAIL] session_generation: GUARD_SESSION_MISSING
[FAIL] margin:             GUARD_MARGIN_UNAVAILABLE
[FAIL] rollback_manifest:  GUARD_MANIFEST_STALE（此記錄修正後應 PASS）
NOT_READY refusal_codes=['GUARD_MANIFEST_STALE','GUARD_MARGIN_UNAVAILABLE',
'GUARD_SESSION_MISSING','GUARD_SNAPSHOT_SOURCE_AMBIGUOUS']
```

## PM2/ecosystem（未修改 — 僅列出待注入）
- `LRC_RELEASE_SHA`: **0 處** — deploy 時必須注入 literal = deploy-time HEAD
- `TRADING_RUNTIME_DIR`: 2 處（preserved，不需改）
- ecosystem.config.js 本 freeze **未修改**

## Runtime certification metadata
<!-- BEGIN RUNTIME_CERTIFICATION_METADATA -->
```json
{
  "broker": {
    "active_orders": 0,
    "capture": "OK",
    "futures_positions": 0,
    "mode": "live",
    "source": "live_broker"
  },
  "callback_registration_generation": 1,
  "candidate_head": "e5c91cfecddf95e8d1a863cd75a2150d351b81f1",
  "captured_at": 1787040118050,
  "clean_worktree": true,
  "config_hash": "e798167389a5c1f42e6697da4e95138434478042eb5087155b826d57320a1c79",
  "durable_locks": {
    "active": 0,
    "retired_unresolved": 5
  },
  "generated_at": "2026-08-18T08:02:00.033942+00:00",
  "live_order_allowed": false,
  "process": {
    "pid": 98987,
    "start_identity": "Tue Aug 18 15:13:57 2026"
  },
  "promotion_mode": "live_quarantined",
  "refresh_generation": "futures-1787040118214403000-529",
  "session_id": "4b46ac5c2f4a0b904d62d6244d1cdfd5",
  "source_tree_hash": "4dff21e05cf9908ba585832372cc3697495c91b432ef913c91ca67815e364c7b",
  "ttl_seconds": 600.0
}
```
<!-- END RUNTIME_CERTIFICATION_METADATA -->
