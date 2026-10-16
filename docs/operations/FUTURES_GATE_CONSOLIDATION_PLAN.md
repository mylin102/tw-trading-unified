# Futures Gate Consolidation Plan（v1 草案）

- 日期：2026-09-15
- 狀態：**草案，待核定**
- 依據：`/tmp/futures_guard_inventory.md`（唯讀盤點，2026-09-15，release16 HEAD `e244c55c`）
- 角色：Hermes（auditor）／ codex（implementer）
- 適用範圍：`core/deployment_safety_gate.py` 的 deployment guards（**38 條 unique**，非 40/70）

---

## 0. 現況（實測）

| 項目 | 實況 |
|---|---|
| production guards | **38 條 unique**（`core/deployment_safety_gate.py`）；使用者提供之「40 unique / 70 call sites」含 tests/docs/策略內部 `ENTRY_GUARD_*`、`POLICY_J_GUARD_*`，非 deployment guard |
| 執行方式 | `check_deployment()` 以 `phase=pre_deploy` 與 `phase=post_startup` **各跑同一組 aggregate checks**（`STARTUP_AUTHORIZATION` 僅 post_startup） |
| 有 runtime 實證者 | **只有 3 條**：`GUARD_POSITION_NOT_FLAT`、`GUARD_TREE_DIRTY`、`GUARD_MANIFEST_STALE`（有事件、**無對應單一 commit SHA**） |
| 其餘 35 條 | **無實證**（防禦性存在）→ **不得據此刪除** |
| F 類（已過時） | **0 條**（`reconciled_exit.py` 的 `BROKER_NOT_FLAT` 字串不是 deployment guard function） |

**殘留型誤擋（本週實際踩到，5 類）**：都是「條件已解決，但 persisted artifact 沒清掉」
1. broker 已 flat/reconcile，ctx 舊 reason（`RESTART_MAINTAIN_QUARANTINE`）仍讓 `GUARD_CTX_*` 拒絕
2. session 已重建、或只是 read-only capture，舊 `SESSION_LOGOUT`/generation 仍在 ctx
3. tree 已修但 manifest 未 re-freeze（`GUARD_MANIFEST_STALE`）
4. 新舊 snapshot 路徑並存（`GUARD_SNAPSHOT_SOURCE_AMBIGUOUS`）
5. 查詢回空被持久化成空 snapshot → 把「證據不可得」與「flat」混淆

---

## 1. 原則（本計畫的邊界）

### 本計畫做什麼
1. **去重（presentation / state duplication）**：同一條件在 pre_deploy + post_startup 重複評估 → 合併為單一評估，兩階段共同消費同一份 **immutable capture / reconciliation result**
2. **狀態改 derived**：ctx 的 mode/reason 由「永久授權狀態」改為「當下結果快照」—— 每次由 reconciliation/session 現況重導出
3. **保留所有安全能力**：broker truth、capture unavailable、session binding、ctx durability **一律保留**

### 紅線（不得跨越）
> 任何移除 `GUARD_POSITION_NOT_FLAT`、capture unavailable、session binding 或 ctx durability 之前，**必須先有等價的 derived evidence 與 regression/replay**。

- 每個現有 refusal code **必須保留**（作為 audit），先映射再切換
- 不得在事故未收斂期間重構 gate（自行鎖死部署能力）

---

## 2. 目標架構（38 → 9）

| # | 目標條 | 收斂原 guards | 階段 |
|---|---|---|---|
| 1 | **RELEASE_IDENTITY** | HEAD_ENV_MISSING / HEAD_GIT_FAILED / HEAD_MISMATCH / TREE_GIT_FAILED / TREE_DIRTY / MANIFEST_MISSING / MANIFEST_STALE / CONFIG_PROFILE / CONFIG_PROFILE_MISMATCH | 1 |
| 2 | **RUNTIME_NAMESPACE** | RUNTIME_ENV_MISSING / RUNTIME_DIR_MISSING / RUNTIME_NOT_WRITABLE / RUNTIME_NAMESPACE / RUNTIME_SECRETS | 1 |
| 3 | **SINGLE_PROCESS** | DUPLICATE_INSTANCE | 1 |
| 4 | **BROKER_CAPTURE_AVAILABILITY** | SNAPSHOT_MISSING / SNAPSHOT_INVALID / CAPTURE_UNAVAILABLE / SNAPSHOT_SOURCE_AMBIGUOUS / SNAPSHOT_PAPER_NOT_LIVE | 2 |
| 5 | **BROKER_RECONCILIATION** | POSITION_NOT_FLAT / PENDING_ORDERS / SNAPSHOT_STALE / SNAPSHOT_HASH_MISSING（+ 未來 legs/side/qty/avg_cost reconciliation）| 2 |
| 6 | **BROKER_EVIDENCE_BINDING** | MARGIN_* / CAPTURE_MISMATCH | 2 |
| 7 | **SESSION_BINDING** | SESSION_INTERVENED / SESSION_MISSING / SESSION_REVOKED | 3 |
| 8 | **CTX_DURABILITY** | CTX_INVALID / CTX_MISSING / CTX_CORRUPT / CTX_WRITE_FAIL（reason 改為當下結果導出）| 3 |
| 9 | **STARTUP_ROUTE_INTEGRITY** | STARTUP_UNSAFE | 3 |

---

## 3. 遷移計畫（每步含驗收點）

### Stage 1（低風險：identity / runtime / process）
- 步驟 1.1 把 1~3 的 refusal codes 映射到新的 derived result（**保留原 code 輸出於 audit**）
- 步驟 1.2 兩階段共用同一份 release identity 評估結果
- **驗收**：① 原 codes 仍出現在 audit ② 38 → 27 條呼叫點的等價性測試（同一輸入 → 同結論同 code）③ 不存在「新 result 放行、舊 rule 會拒絕」的組合（差異清單為空）

### Stage 2（broker truth：摩擦最大群）
- 步驟 2.1 定義 **單一 immutable capture/reconciliation result**（snapshot + margin + reconciliation 綁同一 provenance：account / scope / captured_at / hash）
- 步驟 2.2 pre/post 共同消費該 result（消除 `GUARD_MARGIN_*` 與 `GUARD_CAPTURE_MISMATCH` 的重複重檢）
- 步驟 2.3 `GUARD_POSITION_NOT_FLAT` 改由 reconciliation evidence 導出（existing-MTS 情境由證據判定，非形狀檢查）
- **驗收**：① provenance 不一致 → 拒絕且 reason 明確 ② 查詢不可得 → **UNAVAILABLE（非空/非 flat）** ③ 同一 result 在兩階段結論一致 ④ 不得出現「capture 未取得卻放行」

### Stage 3（session / ctx / startup route）
- 步驟 3.1 session binding：只認 runtime session；read-only capture 明確排除
- 步驟 3.2 ctx reason 改為「當下結果快照」：每次由 reconciliation/session 重導出，**不再永久授權**
- 步驟 3.3 startup route integrity 合併
- **驗收**：① 5 類殘留型誤擋的 regression test（條件已解 → **不再拒絕**）② 條件未解 → 仍 fail-closed ③ 事故 replay（本週 SESSION_LOGOUT / HOLD 未清 / flat 後阻擋）全數還原為正確結論

---

## 4. 必測矩陣

| 類別 | 必測 |
|---|---|
| 等價性 | 每個舊 refusal code ↔ 新 derived result 同輸入同結論 |
| 邊界 | 缺 artifact / 過期 artifact / hash 不符 / account 不符 / scope 不符 / 非 finite 數值 / None |
| 不可得 | 券商查詢失敗、margin None、snapshot 空 → **UNAVAILABLE 且拒絕**（嚴禁當 flat）|
| 殘留 | 「條件已解但 artifact 未清」→ **不得拒絕**（5 類逐條）|
| Replay | 本週事故序列（SESSION_LOGOUT、HOLD 未清、flat 後阻擋、phantom flat）|
| Mutation | 移除任一 derived 條件 → 對應測試必須 **fail** |

---

## 5. 風險與禁止事項

| 風險 | 處置 |
|---|---|
| 為了簡化拆掉救命的 guard | 紅線：先有等價 derived evidence + replay 才能移除；否則只做去重 |
| 事故期間重構自鎖 | 排在事故收斂後；Stage 1 的前置（純映射）可先做 |
| 去重造成兩階段結論不一致 | Stage 1.2 驗收要求差異清單為空 |
| reason 改 derived 後丟失審計 | 原 refusal code 永久保留於 audit 輸出 |
| 過度相信無實證 guard 的價值 | 35 條無實證者**保留**但標註；由 USER 逐條裁定是否合併（不刪） |

---

## 6. 未決事項（需 USER 決定）

1. **是否核定** 9 條目標架構與三階段遷移順序
2. **動工時機**：事故收斂後才動 Stage 2/3？（建議是）；Stage 1 的映射文件可先做
3. **35 條無實證 guards** 的處置原則：全部保留（建議）／逐條合併
4. 是否要求 Stage 2 前先補 **capture provenance 的 immutable artifact 規格**（建議是）
