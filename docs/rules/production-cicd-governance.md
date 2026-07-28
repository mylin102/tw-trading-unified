# Production CI/CD Governance Rule

## 1. Purpose

本規則定義 production commit 的測試、批准、部署與啟動條件，確保：

```text
TESTED ARTIFACT == APPROVED ARTIFACT == DEPLOYED ARTIFACT
```

任何 agent、人工操作或自動化流程均不得僅以：

```text
tests passed
deployed successfully
PM2 online
```

作為 production-safe 的證據。

Production promotion 必須具備：

1. Mandatory Remote CI Gate
2. CI-attested Deployment Manifest
3. Mini fail-closed startup validation
4. 可查核的 deployment evidence

---

# 2. Scope

本規則適用於：

* `tw-trading-unified-git`
* Mini production host
* Air4 development / offline research host
* `trading-system`
* production dashboard
* Shioaji broker integration
* MTS execution、recovery、persistence、risk、configuration 與 deployment 變更

以下變更一律受本規則約束：

* execution logic
* entry / exit policy
* recovery logic
* persistence logic
* broker routing
* position reconciliation
* runtime state schema
* config files
* PM2 configuration
* dependency lockfile
* role-gate logic
* production deployment scripts

---

# 3. Host Authority

## Air4

Air4 的正式角色為：

```text
OFFLINE_RESEARCH / DEVELOPMENT
```

Air4 可以：

* 修改程式碼
* 執行單元測試
* 執行 replay
* 執行 fault-injection tests
* 建立 pull request
* 分析 production export
* 產生研究報告

Air4 不得：

* 建立 Shioaji production session
* 執行 production order
* 被宣稱為 production deployment host
* 以本地測試結果取代 remote CI evidence

## Mini

Mini 的正式角色為：

```text
PRODUCTION
```

Mini 可以：

* 執行 approved production commit
* 連接 broker
* 執行 PM2 services
* 執行 startup reconciliation
* 驗證 deployment manifest
* 執行 production smoke checks

Mini 不得：

* 自行修改 production source code
* 直接部署未經 remote CI 驗證的 commit
* 在 manifest 不符時進入 READY
* 因找不到 manifest、state 或 latch 而假設安全
* 自動部署最新 `master`

---

# 4. Mandatory Remote CI Gate

## 4.1 General Rule

任何可進入 production promotion 的 commit，必須由獨立 remote CI 驗證。

下列資訊不構成有效 CI 證據：

```text
agent 說 tests passed
本地 pytest 通過
Air4 上全部通過
Mini 上手動跑過
26/26 passed
```

除非結果可追溯至：

```text
remote CI run
+
exact commit SHA
+
required jobs
+
machine-verifiable result
```

## 4.2 Required Triggers

Remote CI 至少應在以下事件執行：

```text
pull_request
push to protected branch
manual production promotion workflow
```

## 4.3 Required Blocking Jobs

Production eligibility 至少需要以下 jobs 全部通過：

```text
1. syntax-and-import-check
2. unit-tests
3. recovery-persistence-tests
4. fault-injection-tests
5. config-schema-validation
6. deployment-role-gate-tests
7. production-manifest-build
```

任何 required job：

```text
FAILED
CANCELLED
SKIPPED
TIMED_OUT
MISSING
```

均視為：

```text
PRODUCTION_INELIGIBLE
```

## 4.4 Test-to-Commit Binding

CI 必須明確記錄：

```text
repository
commit SHA
branch or pull request
workflow name
run ID
run attempt
test result
created_at
```

不得使用以下方式宣稱 commit 已通過：

```text
另一個 commit 的 CI 結果
dirty working tree 的測試結果
尚未 push 的本地修改
只跑 subset 但聲稱 full suite passed
```

## 4.5 Fault-Injection Requirement

涉及以下模組的變更，fault-injection tests 為 blocking gate：

* durable append
* atomic state write
* recovery
* settlement
* reconciliation
* failure latch
* sentinel
* broker query failure
* PM2 restart recovery
* order resubmission protection

例如 ADR-024E.1 類型的變更，至少要證明：

```text
任何 write / flush / fsync / replace / directory fsync /
broker query / restart failure

只能導向：

HALT
NOT_READY
INDETERMINATE
RECONCILIATION_REQUIRED

不得導向：

READY
new entry
combined-exit resubmit
silent FLAT
```

---

# 5. CI-Attested Deployment Manifest

## 5.1 Authority

Deployment manifest 必須由 remote CI 根據已驗證 commit 產生。

不得由以下來源單獨建立：

* production agent
* Mini deployment script
* 人工編輯 JSON
* 未受信任的本地腳本

部署端可以附加 deployment-time evidence，但不得自行宣稱：

```json
"test_result": "PASS"
```

## 5.2 Minimum Schema

Manifest 至少包含：

```json
{
  "schema_version": 1,
  "repository": "tw-trading-unified-git",
  "branch": "master",
  "commit_sha": "<full-40-character-sha>",
  "ci_provider": "github-actions",
  "ci_workflow": "production-gate",
  "ci_run_id": "<run-id>",
  "ci_run_attempt": 1,
  "ci_result": "PASS",
  "required_jobs": {
    "syntax-and-import-check": "PASS",
    "unit-tests": "PASS",
    "recovery-persistence-tests": "PASS",
    "fault-injection-tests": "PASS",
    "config-schema-validation": "PASS",
    "deployment-role-gate-tests": "PASS"
  },
  "config_sha256": "<sha256>",
  "lockfile_sha256": "<sha256>",
  "pm2_config_sha256": "<sha256>",
  "deployment_role": "production",
  "target_host": "mini",
  "runtime_schema_version": "<version>",
  "previous_production_commit": "<full-sha>",
  "created_at": "<UTC timestamp>",
  "approved_by": "<human identity>",
  "approved_at": "<UTC timestamp>"
}
```

## 5.3 Approval Rule

Remote CI 通過只代表：

```text
ELIGIBLE_FOR_PROMOTION
```

不代表自動部署。

Production promotion 仍需明確人工批准：

```text
approved_by
approved_at
approved_commit_sha
target_host
```

Agent 不得自行將：

```text
CI PASS
```

解讀為：

```text
APPROVED FOR LIVE TRADING
```

---

# 6. Mini Pre-Deployment Validation

Mini 在 checkout、restart 或進入 READY 前，必須驗證：

```text
1. manifest schema valid
2. manifest CI result == PASS
3. all required jobs == PASS
4. local HEAD == manifest.commit_sha
5. working tree is clean
6. config hash == manifest.config_sha256
7. lockfile hash == manifest.lockfile_sha256
8. PM2 config hash == manifest.pm2_config_sha256
9. deployment role == production
10. target host == mini
11. current host identity == Mini
12. approval exists
13. previous commit matches recorded production lineage
```

任一條件失敗：

```text
DEPLOYMENT_MANIFEST_INVALID
```

系統必須：

```text
entry_enabled = false
combined_exit_resubmit_enabled = false
READY prohibited
manual investigation required
```

不得採用：

```text
warning only
fallback to latest master
ignore mismatch
auto-correct manifest
```

---

# 7. Startup Fail-Closed Contract

Mini 啟動預設值必須是：

```text
entry_enabled = false
combined_exit_resubmit_enabled = false
runtime_ready = false
```

只有下列條件全部成立後，才可考慮進入 READY：

```text
valid deployment manifest
+
valid host role
+
runtime state successfully loaded
+
no unresolved settlement latch
+
no unresolved halt sentinel
+
broker reconciliation completed
+
ledger/runtime consistency accepted
```

以下情況不得被解讀為安全：

```text
manifest missing
state file missing unexpectedly
state file unreadable
latch malformed
sentinel malformed
broker query returns None
broker query timeout
ledger parse error
runtime phase unknown
```

上述情況一律導向：

```text
NOT_READY
HALTED
RECONCILIATION_REQUIRED
或
INDETERMINATE
```

---

# 8. Deployment Procedure

Production deployment 必須使用明確 SHA，不得部署模糊 reference。

允許：

```text
git fetch origin
git switch --detach <approved-full-sha>
```

不得使用：

```text
git pull
git checkout master && use latest
git reset --hard origin/master
```

除非該 SHA 已在 manifest 中明確核准且完全相符。

標準部署流程：

```text
1. fetch remote
2. retrieve CI-attested manifest
3. validate approval
4. validate previous production commit
5. verify clean working tree
6. checkout approved detached SHA
7. verify code/config/lockfile/PM2 hashes
8. back up required runtime state
9. restart PM2 process
10. run startup fail-closed checks
11. run broker/ledger/runtime reconciliation
12. enter READY only after all gates pass
13. emit deployment evidence
```

---

# 9. Deployment Evidence

Agent 回報「已部署」時，至少要提供：

```text
target host
repository path
full commit SHA
previous commit SHA
manifest ID or hash
CI run ID
required CI result
config hash verification
lockfile hash verification
PM2 process name
old PID
new PID
startup state
reconciliation result
READY status
working tree status
deployment timestamp
```

禁止只回報：

```text
已部署
commit: abc123
PID: 12345
tests passed
```

正確狀態範例：

```text
Deployment:
COMPLETE

Commit:
<full SHA>

Manifest verification:
PASS

Required CI jobs:
ALL PASS

Working tree:
CLEAN

PM2 restart:
PASS

Startup state:
NOT_READY → RECONCILING → READY

Broker/ledger/runtime reconciliation:
VERIFIED

Production promotion:
OPERATIONALLY ACTIVE
```

若尚未完成 live observation，必須區分：

```text
DEPLOYED
IMPLEMENTED
CI VERIFIED
STARTUP VERIFIED
OPERATIONALLY VERIFIED
```

不得混用。

---

# 10. Rollback Rule

Rollback 必須使用已知的：

```text
previous_production_commit
```

但交易系統 rollback 不得自動恢復交易。

Rollback 流程：

```text
1. disable entry
2. disable automated resubmit
3. reconcile broker positions
4. preserve current ledger and runtime evidence
5. checkout approved previous SHA
6. restart process
7. validate matching rollback manifest
8. perform startup reconciliation
9. remain NOT_READY until manually approved
```

禁止：

```text
health check fail
→ automatic git rollback
→ automatic READY
```

因為 code rollback 不代表：

```text
broker state
ledger state
runtime schema
```

也已安全回復。

---

# 11. Agent Prohibitions

Agent 不得：

1. 以本地測試取代 remote CI。
2. 以測試數量取代測試範圍證據。
3. 在 dirty working tree 上測試後宣稱該 commit 通過。
4. 部署與 CI 不同的 commit。
5. 自行建立假的 PASS manifest。
6. 在 manifest mismatch 時繼續啟動。
7. 使用最新 `master` 代替 approved SHA。
8. 將 PM2 `online` 解讀為 production READY。
9. 將 PID 改變解讀為部署驗證完成。
10. 在 broker reconciliation 未完成前允許 entry。
11. 遇到未知狀態時採 fail-open。
12. 自動解除 settlement latch、halt sentinel 或 reconciliation requirement。
13. 將 `IMPLEMENTED`、`TESTED`、`DEPLOYED`、`VERIFIED`、`CLOSED` 視為同義詞。

---

# 12. Status Vocabulary

所有 agent 必須使用以下標準狀態：

```text
IMPLEMENTED
程式碼存在，但不代表測試或部署完成。

LOCAL_TESTED
僅本地測試完成。

CI_VERIFIED
該 commit 已通過 mandatory remote CI。

APPROVED
該 commit 已獲 production promotion 批准。

DEPLOYED
該 approved commit 已部署至 Mini。

STARTUP_VERIFIED
Mini 已通過 manifest、state 與 reconciliation gates。

OPERATIONALLY_VERIFIED
已取得 production smoke evidence 或 golden trace。

CLOSED
所有指定 acceptance criteria 均有可查核證據。
```

禁止在缺少 AC 證據時使用：

```text
FULLY CLOSED
PRODUCTION SAFE
FINAL COMPLETE
```

---

# 13. Acceptance Criteria

本規則完成的最低標準：

```text
AC-1  Protected branch requires remote CI.
AC-2  Required CI jobs are blocking.
AC-3  CI result is bound to exact commit SHA.
AC-4  Fault-injection suite is mandatory for persistence changes.
AC-5  CI generates deployment manifest.
AC-6  Human approval is recorded separately from CI PASS.
AC-7  Mini validates code/config/lockfile/PM2 hashes.
AC-8  Manifest mismatch prevents READY.
AC-9  Startup defaults fail-closed.
AC-10 Deployment uses detached approved SHA.
AC-11 Deployment evidence is machine-verifiable.
AC-12 Rollback remains NOT_READY until reconciliation.
```

---

# 14. Core Invariant

所有 CI/CD 設計與實作必須維持以下 invariant：

```text
A commit may reach production only when:

remote CI verified the exact commit
AND
a human approved the exact commit
AND
Mini deployed the exact commit
AND
Mini verified the exact configuration
AND
startup reconciliation passed.
```

任何證據缺失、衝突、不可讀或無法確認時：

```text
FAIL CLOSED
DO NOT ENTER READY
DO NOT PLACE ORDERS
```
