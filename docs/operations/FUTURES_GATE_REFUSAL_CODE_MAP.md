# Futures gate refusal code map（Stage 1 前置文件）

- 狀態：純文件映射，無行為變更
- 來源樹：release16 `e244c55c59d1ec22a91db39cb8508a9671b18fef`
- 目的：描述現有 38 個 refusal code 與 9 個 target derived result 的對應；不刪除、不改名、不改 gate 實作。
- 紅線：所有舊 code 必須持續出現在 audit/refusal evidence；target result 不得在沒有等價證據前取代原 fail-closed 判定。

## 完整映射表

| # | target derived result | 現有 refusal code | 等價性測試計畫 |
|---:|---|---|---|
| 1 | `RELEASE_IDENTITY` | `GUARD_HEAD_ENV_MISSING` | 缺 LRC_RELEASE_SHA → target refused，保留原 code |
| 2 | `RELEASE_IDENTITY` | `GUARD_HEAD_GIT_FAILED` | rev-parse 失敗 → target refused，保留原 code |
| 3 | `RELEASE_IDENTITY` | `GUARD_HEAD_MISMATCH` | actual HEAD != expected → target refused，保留原 code |
| 4 | `RELEASE_IDENTITY` | `GUARD_TREE_GIT_FAILED` | git status 失敗 → target refused，保留原 code |
| 5 | `RELEASE_IDENTITY` | `GUARD_TREE_DIRTY` | closure dirty → target refused，保留原 code |
| 6 | `RELEASE_IDENTITY` | `GUARD_MANIFEST_MISSING` | manifest 缺失 → target refused，保留原 code |
| 7 | `RELEASE_IDENTITY` | `GUARD_MANIFEST_STALE` | frozen hash 與 current tree 不同/無法重算 → target refused，保留原 code |
| 8 | `RELEASE_IDENTITY` | `GUARD_CONFIG_PROFILE` | profile 缺失、不可讀或非 sealed live profile → target refused，保留原 code |
| 9 | `RELEASE_IDENTITY` | `GUARD_CONFIG_PROFILE_MISMATCH` | profile hash/新鮮度不符 → target refused，保留原 code |
| 10 | `RUNTIME_NAMESPACE` | `GUARD_RUNTIME_ENV_MISSING` | runtime env 缺失 → target refused，保留原 code |
| 11 | `RUNTIME_NAMESPACE` | `GUARD_RUNTIME_DIR_MISSING` | runtime 或 logs 目錄缺失 → target refused，保留原 code |
| 12 | `RUNTIME_NAMESPACE` | `GUARD_RUNTIME_NOT_WRITABLE` | runtime/logs 不可寫 → target refused，保留原 code |
| 13 | `RUNTIME_NAMESPACE` | `GUARD_RUNTIME_NAMESPACE` | ctx 不在 canonical env root → target refused，保留原 code |
| 14 | `RUNTIME_NAMESPACE` | `GUARD_RUNTIME_SECRETS` | ctx 含 secret-like key → target refused，保留原 code |
| 15 | `SINGLE_PROCESS` | `GUARD_DUPLICATE_INSTANCE` | pre-deploy 發現活躍 pid → target refused，保留原 code；post-startup attested pid 的既有例外需等價測試 |
| 16 | `BROKER_CAPTURE_AVAILABILITY` | `GUARD_SNAPSHOT_MISSING` | snapshot 不存在 → target `UNAVAILABLE`/refused，保留原 code |
| 17 | `BROKER_CAPTURE_AVAILABILITY` | `GUARD_SNAPSHOT_INVALID` | snapshot JSON 或必要欄位無效 → target refused，保留原 code |
| 18 | `BROKER_CAPTURE_AVAILABILITY` | `GUARD_CAPTURE_UNAVAILABLE` | capture_error 存在 → target `UNAVAILABLE`/refused，保留原 code |
| 19 | `BROKER_CAPTURE_AVAILABILITY` | `GUARD_SNAPSHOT_SOURCE_AMBIGUOUS` | source/mode 缺失或非 live_broker → target refused，保留原 code |
| 20 | `BROKER_CAPTURE_AVAILABILITY` | `GUARD_SNAPSHOT_PAPER_NOT_LIVE` | paper evidence 供 live context 使用 → target refused，保留原 code |
| 21 | `BROKER_RECONCILIATION` | `GUARD_POSITION_NOT_FLAT` | 非零 futures position 且無合格 reconciled evidence → target refused，保留原 code |
| 22 | `BROKER_RECONCILIATION` | `GUARD_PENDING_ORDERS` | working/pending order 存在 → target refused，保留原 code |
| 23 | `BROKER_RECONCILIATION` | `GUARD_SNAPSHOT_STALE` | captured_at 缺失或超過 TTL → target refused，保留原 code |
| 24 | `BROKER_RECONCILIATION` | `GUARD_SNAPSHOT_HASH_MISSING` | canonical input hash 缺失 → target refused，保留原 code |
| 25 | `BROKER_EVIDENCE_BINDING` | `GUARD_MARGIN_UNAVAILABLE` | margin None/查詢不可得 → target `UNAVAILABLE`/refused，保留原 code |
| 26 | `BROKER_EVIDENCE_BINDING` | `GUARD_MARGIN_INVALID` | margin 非 finite、非正數或不可解析 → target refused，保留原 code |
| 27 | `BROKER_EVIDENCE_BINDING` | `GUARD_MARGIN_INSUFFICIENT` | margin 小於既有 floor → target refused，保留原 code |
| 28 | `BROKER_EVIDENCE_BINDING` | `GUARD_MARGIN_EVIDENCE_MISSING` | account/scope/captured_at/hash 缺任一 → target refused，保留原 code |
| 29 | `BROKER_EVIDENCE_BINDING` | `GUARD_MARGIN_EVIDENCE_STALE` | margin evidence timestamp 無效或過期 → target refused，保留原 code |
| 30 | `BROKER_EVIDENCE_BINDING` | `GUARD_CAPTURE_MISMATCH` | snapshot/margin hash、時間、account 或 scope 不一致 → target refused，保留原 code |
| 31 | `SESSION_BINDING` | `GUARD_SESSION_INTERVENED` | runtime-bound snapshot session != ctx session → target refused，保留原 code；read-only capture 不得觸發 |
| 32 | `SESSION_BINDING` | `GUARD_SESSION_MISSING` | post-startup 缺 registry-bound generation 或格式錯 → target refused，保留原 code |
| 33 | `SESSION_BINDING` | `GUARD_SESSION_REVOKED` | generation 已 revoked → target refused，保留原 code |
| 34 | `CTX_DURABILITY` | `GUARD_CTX_INVALID` | ctx 無可用 mode/reason → target refused，保留原 code |
| 35 | `CTX_DURABILITY` | `GUARD_CTX_MISSING` | persisted restart-maintain-quarantine reason 映射為 missing → target refused，保留原 code |
| 36 | `CTX_DURABILITY` | `GUARD_CTX_CORRUPT` | state file corrupted reason → target refused，保留原 code |
| 37 | `CTX_DURABILITY` | `GUARD_CTX_WRITE_FAIL` | atomic write/read-back probe 失敗 → target refused，保留原 code |
| 38 | `STARTUP_ROUTE_INTEGRITY` | `GUARD_STARTUP_UNSAFE` | preflight/cert wiring 缺失、AST 解析失敗或三個 order route gate 不完整 → target refused，保留原 code |

## 一對多／多對一說明

- 本階段是多對一：38 個 legacy refusal code 映射至 9 個 target derived result。
- `BROKER_CAPTURE_AVAILABILITY` 的 `MISSING`、`INVALID`、`UNAVAILABLE`、`SOURCE_AMBIGUOUS`、`PAPER_NOT_LIVE` 必須保留不同 audit detail；不得只留下模糊的 `UNAVAILABLE`。
- `BROKER_RECONCILIATION` 必須保留 position、working-order、freshness、hash 等 detail；未來 legs/side/qty/avg_cost reconciliation 也歸入此 derived result，但不得取代既有 code 的 audit record。
- `BROKER_EVIDENCE_BINDING` 的 margin 與 capture mismatch 必須保留 provenance 欄位與原拒絕碼；不可用一個成功的 margin 數值掩蓋 capture 不一致。
- `RELEASE_IDENTITY`、`RUNTIME_NAMESPACE`、`SESSION_BINDING` 與 `CTX_DURABILITY` 都是多個舊 code 對一個 target result；target 結果只負責聚合，舊 code 是可追溯的原因。
- 本文件未新增 legacy code，也未將任何 legacy code 標為 obsolete。

## 等價性測試計畫（僅規格，不執行行為修改）

1. 對每一列建立輸入 fixture：只改變該列對應的失敗條件，其餘輸入保持一個已知可通過 baseline。
2. 同一 fixture 同時送入 legacy guard 與 target derived evaluator；兩者必須同為拒絕，且 output audit 必須包含原 legacy refusal code。
3. 對每一個 target result 建立全數 legacy member 的 parameterized matrix，確認多對一聚合不會漏掉任一拒絕分支。
4. 對 `BROKER_CAPTURE_AVAILABILITY`、`BROKER_RECONCILIATION`、`BROKER_EVIDENCE_BINDING` 加入 `None`、空回應、過期、hash/account/scope mismatch 的 fail-closed cases；不可把 unavailable 當 flat。
5. 對 pre-deploy 與 post-startup 執行相同 immutable evidence，確認 target conclusion 相同；`SESSION_BINDING` 另驗 pre-deploy NOT_ASSESSED 與 post-startup required 的既有語意。
6. 對 read-only capture 驗證不增加 runtime session generation；對真正 session revoke/intervention 驗證仍拒絕。
7. 對五類 persisted-state residue replay：flat 後舊 HOLD、SESSION_LOGOUT 殘留、manifest stale、snapshot source 分歧、query empty/unavailable artifact；條件解決時不得持續誤擋，條件未解時仍 fail-closed。
8. Mutation：逐一移除 target derived condition，對應 legacy-member test 必須 fail；未通過前不得刪除、改名或停止輸出任何舊 code。

## 邊界與核定事項

- 這是 Stage 1 前置映射文件，不是實作授權。
- 不改 `core/deployment_safety_gate.py`，不改 guard 名稱，不改 refusal code，不改 aggregate semantics。
- 9 個 target result、三階段遷移順序、35 個無 runtime 實證 guards 的處置，仍依 `FUTURES_GATE_CONSOLIDATION_PLAN.md` §6 待 USER 核定。
