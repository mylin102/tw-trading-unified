# Futures deployment guard inventory

唯讀盤點日期：2026-09-15（Asia/Taipei）  
目標樹：Mini `/Users/myllin_mini/Documents/mylin102/tw-trading-unified-release16`  
盤點 HEAD：`e244c55c59d1ec22a91db39cb8508a9671b18fef`

## 盤點範圍與數字校正

本表以 production implementation `core/deployment_safety_gate.py` 為主，並讀取 Mini runtime audit/log；tests、docs、策略內部的 `ENTRY_GUARD_*` / `POLICY_J_GUARD_*` 不算 deployment `GUARD_*`。

在上述 HEAD，程式中可直接枚舉到 **38 個 unique deployment guards**；因此使用者提供的「40 unique / 70 call sites」與目前 release16 HEAD 不一致。本報告不臆造缺少的 2 個名稱，也不把 docs/tests 中的引用誤算成 production guard。`check_deployment()` 每個 phase 呼叫同一組 aggregate checks；同一 guard 的所有 branch line 已列在下表。Runtime log 的 evidence 只在「可定位事件」欄記錄，沒有對應 artifact 的一律寫「無實證」。

分類：A 部署衛生／identity；B broker truth；C session；D ctx 狀態機產物；E 其他；F 已過時。

## Guard table

| guard | 檢查位置（release16） | 觸發條件 | 實證 | 分類 | 建議 | 風險 |
|---|---|---|---|---|---|---|
| GUARD_HEAD_ENV_MISSING | `core/deployment_safety_gate.py:109`; `check_deployment:827-851` | expected SHA/LRC_RELEASE_SHA 缺失、格式錯 | 無實證 | A | KEEP | 未綁定 release，可能啟動未驗證樹 |
| GUARD_HEAD_GIT_FAILED | `:112`; `check_deployment:827-851` | rev-parse 失敗或 HEAD 非 40 hex | 無實證 | A | KEEP | 無法證明實際執行樹 |
| GUARD_HEAD_MISMATCH | `:114`; `check_deployment:827-851` | runtime HEAD != expected SHA | 無實證 | A | KEEP | 執行錯版本 |
| GUARD_TREE_GIT_FAILED | `:130`; `check_deployment:827-851` | git status 失敗 | 無實證 | A | KEEP | 無法證明 closure clean |
| GUARD_TREE_DIRTY | `:133`; `check_deployment:827-851` | closure 有 tracked/untracked dirty entry | Runtime log 曾記錄 `GUARD_MANIFEST_STALE, GUARD_TREE_DIRTY`；無單一 commit 對應事故 | A | KEEP | 未審查的程式/設定可能進 runtime |
| GUARD_RUNTIME_ENV_MISSING | `:158`; `check_deployment:827-851` | TRADING_RUNTIME_DIR 或 runtime_dir 缺 | 無實證 | A | KEEP | 可能 fallback 到錯誤 runtime |
| GUARD_RUNTIME_DIR_MISSING | `:161,165`; `check_deployment:827-851` | runtime/logs 不存在 | 無實證 | A | KEEP | ctx/log 寫入位置不可信 |
| GUARD_RUNTIME_NOT_WRITABLE | `:169`; `check_deployment:827-851` | runtime 或 logs 不可寫 | 無實證 | A | KEEP | state/ctx 可能停寫 |
| GUARD_RUNTIME_NAMESPACE | `:176`; `check_deployment:827-851` | ctx 不在 env 指定 canonical runtime root | 無實證 | A | KEEP | 讀寫兩棵 runtime tree，造成 stale |
| GUARD_RUNTIME_SECRETS | `:180`; `check_deployment:827-851` | ctx JSON key 命中 secret pattern | 無實證 | A | KEEP | credentials 洩漏到 ctx |
| GUARD_DUPLICATE_INSTANCE | `:201,206`; `check_deployment:827-851` | pre-deploy pid 存活，或 permission denied 視為存活 | 無實證 | A | KEEP | 多 process 競爭送單/寫 state |
| GUARD_SNAPSHOT_MISSING | `:253`; `check_deployment:827-851` | canonical broker snapshot 不存在 | 無實證 | B | KEEP | 缺 broker truth 卻可能誤判 flat |
| GUARD_SNAPSHOT_INVALID | `:257,286`; `check_deployment:827-851` | JSON 壞、positions/open_orders 缺欄位 | 無實證 | B | KEEP | 解析錯誤被當成 flat |
| GUARD_CAPTURE_UNAVAILABLE | `:260`; `check_deployment:827-851` | snapshot 有 capture_error | 無實證 | B | KEEP | 查詢失敗被當成空倉 |
| GUARD_SNAPSHOT_PAPER_NOT_LIVE | `:271`; `check_deployment:827-851` | paper snapshot 用於 live context | 無實證 | B | KEEP | paper evidence 冒充 live broker truth |
| GUARD_SNAPSHOT_SOURCE_AMBIGUOUS | `:276`; `check_deployment:827-851` | source/mode 不是明確 `live_broker` 或缺失 | 無實證 | B | KEEP | 舊/新 capture 路徑混用，錯誤 flat |
| GUARD_POSITION_NOT_FLAT | `:340,344`; `check_deployment:827-851` | futures positions 有非零 qty；existing-MTS exception 不成立 | Runtime log：`POST_STARTUP_GATE_FAILED ... GUARD_POSITION_NOT_FLAT`；另有 user-reported reconciled spread incident；無對應單一 commit SHA | B | KEEP；未來由 reconciliation evidence derive | 移除會讓未對帳持倉進入 live/重複下單 |
| GUARD_PENDING_ORDERS | `:347`; `check_deployment:827-851` | open/pending futures orders 非空 | 無實證 | B | KEEP | 重啟後與 broker working order 雙重送單 |
| GUARD_SNAPSHOT_STALE | `:350,353`; `check_deployment:827-851` | captured_at 缺失或超過 600 秒 | 無實證 | B | KEEP | 使用過期倉位/訂單真值 |
| GUARD_SNAPSHOT_HASH_MISSING | `:357`; `check_deployment:827-851` | canonical_input_hash 缺失 | 無實證 | B | KEEP | snapshot provenance 不可驗 |
| GUARD_SESSION_INTERVENED | `:365`; `:818`（post-startup 說明）；`check_deployment:827-851` | runtime-bound snapshot session != ctx session；read-only capture 明確排除 | Runtime log 有多次 `Session logout: ctx QUARANTINED`，但未將此 guard code 單獨寫出；無此 guard 的直接證據 | C | KEEP；與 session generation 合併呈現 | 外部 session 變更後仍可能送單 |
| GUARD_STARTUP_UNSAFE | `:391,395,413`; `check_deployment:827-851` | monitor 缺 preflight/cert wiring、AST parse 失敗、三個 order route gate 不完整 | 無實證 | E | KEEP；可合併為 runtime route-integrity | startup bypass safety stop |
| GUARD_SESSION_MISSING | `:430,434,438`; `check_deployment:827-851` | post-startup 沒有 registry-bound generation，或格式不是 32-hex registry token | Runtime log 的 SESSION logout 事件未帶此 refusal code；無直接實證 | C | KEEP；MERGE 至 session lifecycle | session identity 未綁定卻允許 live |
| GUARD_SESSION_REVOKED | `:442`; `check_deployment:827-851` | registry generation revoked | 無實證 | C | KEEP；MERGE 至 session lifecycle | revoked session 仍可送單 |
| GUARD_MARGIN_UNAVAILABLE | `:453`; `check_deployment:827-851` | margin 為 None | 曾出現 user-reported `GUARD_MARGIN_UNAVAILABLE` gate blocker；runtime direct artifact 未找到 | B | KEEP | margin 查詢失敗被當成可用 |
| GUARD_MARGIN_INVALID | `:457,459`; `check_deployment:827-851` | margin 非數字、非 finite、<= 0 | 無實證 | B | KEEP | 資金數值錯誤可能放行風險 |
| GUARD_MARGIN_INSUFFICIENT | `:461`; `check_deployment:827-851` | margin < configured floor | 無實證 | B | KEEP | 資金不足仍進 live |
| GUARD_MARGIN_EVIDENCE_MISSING | `:467,474`; `check_deployment:827-851` | 缺 account identity/scope/captured_at/hash | 無實證 | B | KEEP | bare margin 冒充同一 authenticated capture |
| GUARD_MARGIN_EVIDENCE_STALE | `:482,485`; `check_deployment:827-851` | evidence timestamp 無效或超過 600 秒 | 無實證 | B | KEEP | 使用過期資金證據 |
| GUARD_MANIFEST_MISSING | `:533`; `check_deployment:827-851` | rollback manifest 不存在 | 無實證 | A | KEEP | 不知道 frozen release identity |
| GUARD_MANIFEST_STALE | `:505,545,550,553`; `check_deployment:827-851` | frozen_tree_hash 缺失、無法重算、或 current tree drift | Runtime log 曾記錄 `GUARD_MANIFEST_STALE, GUARD_TREE_DIRTY`；無單一事故 commit 對應 | A | KEEP | code drift 未被 freeze/gate 發現 |
| GUARD_CONFIG_PROFILE | `:580,583,588,594,599`; `check_deployment:827-851` | profile 缺失/不可讀/非 sealed futures_live 或 live_trading 非 true | 無實證 | A | KEEP | paper/未核定 profile 被當 live |
| GUARD_CONFIG_PROFILE_MISMATCH | `:607,612`; `check_deployment:827-851` | profile hash 或 captured_at 不符/過期 | 無實證 | A | KEEP | profile 被改而未重新認證 |
| GUARD_CAPTURE_MISMATCH | `:631,634,640,649,657,661,668,671,674,677`; `check_deployment:827-851` | snapshot 與 margin capture 的 hash/time/account/scope 不同、缺失或過期 | 曾出現 user-reported `GUARD_CAPTURE_MISMATCH` blocker；無 runtime 直接事件 artifact | B | KEEP；與 capture provenance 併為單一 derived result | 不同時點/帳號的證據被拼接 |
| GUARD_CTX_INVALID | `:690`; `check_deployment:827-851` | ctx 無 audit reasons 且 effective_mode 也缺失 | 無實證 | D | KEEP | 損壞 ctx 被當正常 |
| GUARD_CTX_WRITE_FAIL | `:699,712,724`; `check_deployment:827-851` | atomic write probe 失敗 | 無實證 | D | KEEP | state/ctx 停寫且下游看到 stale |
| GUARD_CTX_MISSING | `:718`; `check_deployment:827-851` | persisted reason `RESTART_MAINTAIN_QUARANTINE` 映射為 missing | User-reported stale/quarantine history；未找到該 refusal 的直接 log 行 | D | DERIVE from current ctx + reconciliation; retain fail-closed | 移除可能把缺 ctx 當 live |
| GUARD_CTX_CORRUPT | `:719`; `check_deployment:827-851` | persisted reason `STATE_FILE_CORRUPTED` | 無實證 | D | KEEP | corrupted state 造成錯誤持倉/下單 |

## 多階段重複圖

`check_deployment()` 對同一組 guards 以 `phase=pre_deploy` 與 `phase=post_startup` 各跑一次；因此下列不是兩份不同規則，而是同一條規則在兩個 lifecycle checkpoint 重複驗證：

```text
pre_deploy
  RELEASE_INTEGRITY: HEAD / TREE / MANIFEST / CONFIG_PROFILE
  RUNTIME_READY: runtime paths / single process / ctx atomic / startup AST
  BROKER_TRUTH: snapshot(flat+orders) / margin / capture consistency
  STARTUP_AUTHORIZATION: NOT_ASSESSED
        │ restart/login
        ▼
post_startup
  同一組 RELEASE_INTEGRITY + RUNTIME_READY + BROKER_TRUTH 再驗一次
  STARTUP_AUTHORIZATION: session generation + revoked
  flat guard 內另檢查 runtime-bound snapshot session
```

具體重複群：

- `GUARD_HEAD_*`, `GUARD_TREE_*`, `GUARD_RUNTIME_*`, `GUARD_DUPLICATE_INSTANCE`, `GUARD_STARTUP_UNSAFE`：pre/post 都會被 aggregate pipeline 呼叫；single process 在 post-startup 有 attested-pid 例外。
- `GUARD_SNAPSHOT_*`, `GUARD_POSITION_NOT_FLAT`, `GUARD_PENDING_ORDERS`, `GUARD_SNAPSHOT_STALE`, `GUARD_SNAPSHOT_HASH_MISSING`：flat snapshot 於兩階段重算。
- `GUARD_MARGIN_*` 與 `GUARD_CAPTURE_MISMATCH`：兩階段重驗同一 capture provenance；目前是摩擦最大的 broker-truth 重複群。
- `GUARD_CTX_*`：兩階段都讀 ctx 並做 atomic write probe；持久化 reason 未被新 reconciliation 結果取代時，會持續重複拒絕。
- `GUARD_SESSION_MISSING/REVOKED`：只有 post-startup 實際評估；pre-deploy 明確 NOT_ASSESSED，不算誤重複。

## 殘留型誤擋

以下是「條件已解決，但 persisted artifact 沒清掉」會持續誤擋的類型；它們不是應被刪除的安全證據，而是應改成由當下 reconciliation/session 結果重導出的拒絕理由：

1. `GUARD_POSITION_NOT_FLAT`／ctx 的 `RESTART_MAINTAIN_QUARANTINE`：broker 已 reconcile/flat 後，舊 ctx reason 仍可讓 `GUARD_CTX_MISSING` 或 `GUARD_CTX_*` 持續拒絕。已知本週 flat 後 HOLD 未清案例；直接 log 有 position-not-flat quarantine。
2. `GUARD_SESSION_INTERVENED`／`GUARD_SESSION_MISSING`／`GUARD_SESSION_REVOKED`：runtime session 已重新建立或只是 read-only capture，舊 `SESSION_LOGOUT`/generation 仍留在 ctx，形成 persisted false positive。runtime log 多次實際記錄 `Session logout: ctx QUARANTINED`。
3. `GUARD_MANIFEST_STALE`／`GUARD_TREE_DIRTY`：tree 已完成修正但 manifest 未 re-freeze，或 runtime artifacts 留在 closure；log 有兩者同時拒絕的實例。
4. `GUARD_SNAPSHOT_SOURCE_AMBIGUOUS`／`GUARD_CAPTURE_MISMATCH`：舊 diagnostics path 與 canonical live path 並存，capture 與 gate 讀到不同 artifact；應收斂 source，不應降低 gate。
5. `GUARD_CAPTURE_UNAVAILABLE`／`GUARD_MARGIN_UNAVAILABLE`：查詢回空被持久化成空 snapshot 時，後續會把「證據不可得」與「flat」混淆；修復方向是顯式 unavailable，不能清成 flat。

## 收斂提案（8–10 條）

目標是減少 presentation/state duplication，不移除 fail-closed 證據。

| 目標條 | 收斂原 guards | 建議順序 |
|---|---|---|
| 1. RELEASE_IDENTITY | HEAD_ENV/GIT_FAILED/MISMATCH + TREE_GIT_FAILED/DIRTY + MANIFEST_MISSING/STALE + CONFIG_PROFILE/MISMATCH | 1 |
| 2. RUNTIME_NAMESPACE | RUNTIME_ENV/DIR_MISSING/NOT_WRITABLE/NAMESPACE/SECRETS | 1 |
| 3. SINGLE_PROCESS | DUPLICATE_INSTANCE | 1 |
| 4. BROKER_CAPTURE_AVAILABILITY | SNAPSHOT_MISSING/INVALID/CAPTURE_UNAVAILABLE/SOURCE_AMBIGUOUS/PAPER_NOT_LIVE | 2 |
| 5. BROKER_RECONCILIATION | POSITION_NOT_FLAT/PENDING_ORDERS/SNAPSHOT_STALE/HASH_MISSING + future legs/side/qty/avg_cost reconciliation | 2 |
| 6. BROKER_EVIDENCE_BINDING | MARGIN_* + CAPTURE_MISMATCH | 2 |
| 7. SESSION_BINDING | SESSION_INTERVENED/MISSING/REVOKED | 3 |
| 8. CTX_DURABILITY | CTX_INVALID/MISSING/CORRUPT/WRITE_FAIL；reason 每次由 reconciliation/session 當下結果重導出 | 3 |
| 9. STARTUP_ROUTE_INTEGRITY | STARTUP_UNSAFE | 3 |

遷移順序：先把每個現有 refusal code 映射到新 derived result 並保留原 code 做 audit；再讓 pre/post 共同消費同一 immutable capture/reconciliation result；接著把 ctx reason 改成結果快照而非永久授權狀態；最後才移除重複 presentation。任何移除 `POSITION_NOT_FLAT`、capture unavailable、session binding 或 ctx durability 前，必須先有等價 derived evidence 與 regression/replay。

## F 類與證據不足

### F（已過時）

目前 release16 production deployment gate 中：**無可安全列為 F 的 guard**。`reconciled_exit.py` 仍能看到 `BROKER_NOT_FLAT`/`GUARD_POSITION_NOT_FLAT` 字串，但那不是 deployment guard function；也沒有在本 HEAD 的 `deployment_safety_gate.py` 發現 `exit-only` 或 `quarantine-only` guard 可直接標為 obsolete。`GUARD_STARTUP_UNSAFE` 仍是 active AST safety check，不列 F。

### 證據不足，待 USER 決定

下列 guards 在本次唯讀資料中沒有「該 guard refusal code + 日期/事件 + 對應 commit SHA」的完整三件套，因此不把 user-reported blocker 擴寫成實證：

- `GUARD_HEAD_ENV_MISSING`, `GUARD_HEAD_GIT_FAILED`, `GUARD_HEAD_MISMATCH`
- `GUARD_RUNTIME_ENV_MISSING`, `GUARD_RUNTIME_DIR_MISSING`, `GUARD_RUNTIME_NOT_WRITABLE`, `GUARD_RUNTIME_NAMESPACE`, `GUARD_RUNTIME_SECRETS`
- `GUARD_DUPLICATE_INSTANCE`
- `GUARD_SNAPSHOT_MISSING`, `GUARD_SNAPSHOT_INVALID`, `GUARD_CAPTURE_UNAVAILABLE`, `GUARD_SNAPSHOT_PAPER_NOT_LIVE`, `GUARD_SNAPSHOT_SOURCE_AMBIGUOUS`
- `GUARD_PENDING_ORDERS`, `GUARD_SNAPSHOT_STALE`, `GUARD_SNAPSHOT_HASH_MISSING`
- `GUARD_SESSION_INTERVENED`, `GUARD_SESSION_MISSING`, `GUARD_SESSION_REVOKED`
- `GUARD_STARTUP_UNSAFE`
- 全部 `GUARD_MARGIN_*`
- `GUARD_CAPTURE_MISMATCH`
- `GUARD_CTX_INVALID`, `GUARD_CTX_WRITE_FAIL`, `GUARD_CTX_MISSING`, `GUARD_CTX_CORRUPT`

`GUARD_POSITION_NOT_FLAT`、`GUARD_TREE_DIRTY`、`GUARD_MANIFEST_STALE` 有 runtime log evidence，但現有 logs 沒有把每一筆事件綁到一個唯一 commit SHA；故表內明確保留「有事件、SHA 不可得」，沒有虛構 SHA。

## 唯讀證據摘要

- release16 HEAD：`e244c55c59d1ec22a91db39cb8508a9671b18fef`
- production guard implementation：`core/deployment_safety_gate.py`
- aggregate groups：`RELEASE_INTEGRITY`, `RUNTIME_READY`, `BROKER_TRUTH`, `STARTUP_AUTHORIZATION`
- runtime 可見事件：`GUARD_POSITION_NOT_FLAT`、`GUARD_MANIFEST_STALE` + `GUARD_TREE_DIRTY`、多次 `Session logout: ctx QUARANTINED`
- 本次沒有部署、重啟、broker call、ctx/state/ledger write。
