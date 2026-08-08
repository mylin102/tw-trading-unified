# Live Route Certification — Phase 2 設計（monitor wiring，design + RED tests only）

**狀態**: DESIGN（codex Phase-1 ACCEPTED 後授權；本 phase 只交付 code map +
test matrix + RED tests，**不 edit monitor/config/ecosystem**）
**前置**: Phase 1 core（3320e122..6bb6de1d, 96+6+78 tests green）—
`core/live_route_certificate.py` 已 ACCEPTED。
**authority projection 維持凍結；不觸碰 dirty files。**

## 1. Code map（MTS LIVE routes — 實證 call sites）

| # | 位置 | 現況 | Phase-2 目標 |
|---|---|---|---|
| 1 | `strategies/futures/monitor.py:512-532` | LIVE 啟動: live_preflight_context → **preflight_validate + `transition_to_live_ready(ctx, failures)` @:522**（legacy 旁路，已被 core 改為 no-cert → LIVE_QUARANTINED） | 改為: fresh in-process `certify_route(...)` → `RuntimeCertificationContext` → `transition_with_certificate(...)`；任何 failure → LIVE_QUARANTINED + 持久化 dashboard-safe reason（audit_reasons → 既有 dashboard 讀取路徑） |
| 2 | `monitor.py:4593-4598` `_submit_mts_order_signal` | `_exec_ctx.assert_live_order_allowed()`（LIVE gate，已存在） | gate 維持 — cert 缺/失效時 ctx 已在 LIVE_QUARANTINED → gate raise（零送單） |
| 3 | `monitor.py:2707 / 3847` `api.place_order` / `client.place_order` | 直接送單路徑 | 全部必須先過 `assert_live_order_allowed()`（PAPER 不變 — gate 在 PAPER 不擋） |
| 4 | `main.py:693-733` `_try_shioaji_reconnect` → `safe_login` @:733 | reconnect 登入（safe_login hook 已註冊新 generation → 舊 cert 自動失效） | reconnect 後 **重新 certify**（舊 cert 因 generation 改變已死）→ 失敗 → QUARANTINED |
| 5 | `main.py:1520` `logout()`（shioaji_session.logout） | logout hook 已 unregister + global revocation fallback | cert 隨之失效；下次啟動 = fresh certify |
| 6 | 啟動順序（monitor LIVE 路徑） | — | **certify 前**驗證 `cwd/HEAD == LRC_RELEASE_SHA`（§4）；缺 env/不符 → QUARANTINED |

## 2. 六區 test matrix（Phase-2 RED，monitor wiring phase 才轉綠）

| 區 | RED tests（本 phase 交付） | 轉綠條件（wiring phase） |
|---|---|---|
| (1) LIVE transition/order route | AST: monitor.py 無 `transition_to_live_ready`；monitor 引用 `transition_with_certificate`/`certify_route`；order routes 前有 cert gate | monitor.py:522 換成 certificate flow；order 路徑全過 gate |
| (2) PAPER unchanged | test_paper_path_unchanged（GREEN 已存在） | 維持 — 不得改變 |
| (3) Release deployment contract | monitor 啟動引用 `LRC_RELEASE_SHA` 驗證；`cwd/HEAD == env` 不一致 → QUARANTINED | 部署注入 env + 啟動驗證接線 |
| (4) TMF margin-floor config | `config/futures.yaml` 含 `mts.live_required_margin_per_pair`（owner/version 註記） | config key 加入（獨立 review） |
| (5) startup/reconnect/logout/restart | startup LIVE 路徑引用 certify_route；reconnect 後重新 certify；logout 後無 cert 可 transition；restart 一律 fresh certify | 對應接線 |
| (6) 零 order/cancel/modify during certification | certification 全程 recording api 零 order 呼叫（core 已有, 96 tests） | 維持 — wiring 不得引入 order 呼叫 |

## 3. 失敗語意（LIVE gate 契約）

- certify/transition 任一失敗 → `ModeTransitionState.LIVE_QUARANTINED` +
  `audit_reasons`（已含: AUTH_SESSION_UNAVAILABLE / MARGIN_INSUFFICIENT /
  REQUIRED_QUERY_FAILURE / QUOTE_SUBSCRIPTION_FAILED / SNAPSHOT_CODES_INCONSISTENT /
  CERT_TAMPERED / SESSION_GENERATION_MISMATCH / SOURCE_MISMATCH / NONCE_UNKNOWN…）
- **persisted dashboard-safe reason**: audit_reasons 寫入既有 dashboard
  讀取路徑（`_execution_context.to_dict()` 已含 audit_reasons —
  dashboard 顯示 READY/QUARANTINED 狀態，無敏感 broker 資料）

## 4. Release deployment contract（Phase-2 #3，wiring phase 實作）

- 部署程序設定 `LRC_RELEASE_SHA` = release 目錄 commit（`git rev-parse HEAD`）
- 啟動 certification 前: `git rev-parse HEAD`（cwd=release 目錄）== env → 否 →
  LIVE_QUARANTINED（`RELEASE_IDENTITY_MISMATCH` reason）
- 本 phase 不 edit ecosystem.config.js（monitor wiring phase 一併處理）

## 5. Acceptance（Phase-2 RED 全綠 = wiring 完成）

1. monitor 無 legacy `transition_to_live_ready` 呼叫（AST）
2. LIVE 啟動 = certify_route → context → transition_with_certificate
3. 失敗 → QUARANTINED + audit_reasons 持久化（dashboard-safe）
4. PAPER 行為零改變（既有測試維持 GREEN）
5. reconnect/logout/restart 的 cert 生命週期正確（失效 → 重新 certify）
6. certification 全程零 order/cancel/modify
7. 全測試: core 96 + safe_login 6 + mode-transition 78 + integration（RED→GREEN）+ audit 34


## 7. v2 修訂（codex Phase-2 review — 6 點）

### 7.1 Exhaustive AST route inventory（修訂 §1）
實證（round-11 全量掃描 monitor.py + core/order_management/order_manager.py）:

| L# | call | enclosing fn | gate 支配 | 判定 |
|---|---|---|---|---|
| 2707 | api.place_order | `_place_safety_stop` | **無** | RED — safety-stop 建立必須 gate 或顯式 emergency |
| 2721 | api.cancel_order | `_cancel_safety_stop` | **無** | RED — cancel 亦 state-changing |
| 3847 | client.place_order | `_submit_order_via_manager` | OrderManager.submit gate（order_manager.py:42-43, 514） | 下游 gate ✓（allowlist） |
| 4598 | dispatcher gate | `_submit_mts_order_signal` | assert_live_order_allowed ✓ | 已 gate |
| 5208 | client.place_order | `_execute_trade`（non-manager） | **無** | RED — 漏列入口（codex 發現） |

- RED 測試: 每個 state-changing call 的支配路徑必須含 gate
  （enclosing fn 內 assert_live_order_allowed，或列名 downstream-gated
  allowlist {3847→OrderManager.submit}，或顯式 EMERGENCY marker）
- update_order/modify_order 同步列入 inventory（現況 0 sites）

### 7.2 Quarantine 語意 + 顯式 emergency path
- gate 擋**所有正常單**（含 EXIT — 不因「出場」而放行）
- **emergency path 已存在（實證）**: `monitor.py:7519 _emergency_flatten_mts`
  （marker `EMERGENCY_FLATTEN` @7549, caller @7096）— wiring phase 需驗證
  其 **durable/idempotent/獨立授權** 三性質（audit ledger 寫入 + 冪等重送）
- safety-stop 於 quarantine/reconnect 期間: **不建立新 stop**（2707 需 gate
  或 emergency 路由）；取消/修復僅經定義 recovery（emergency 指令）

### 7.3 Reconnect atomic handoff（修訂 §1 #4）
現況: `_try_shioaji_reconnect`（main.py:693）safe_login/subscribe 後直接
return True — **無 recertification**。
目標（wiring phase）:
1. re-login **前**: invalidate/quarantine fm execution context
   （ctx → LIVE_QUARANTINED, 舊 cert 死）
2. resubscribe 完成後才 fresh certify_route → transition_with_certificate
3. 期間無 tick 可下單（ctx 已 quarantine → gate raise）
- auto-reconnect branch（code 12, main.py:710-712）同 handoff —
  RED 測試覆蓋 manual + auto 兩分支

### 7.4 Dashboard persistence（修訂 §3 — to_dict 僅記憶體）
- **persistence 點（wiring phase 新增）**: `/tmp/futures_execution_context.json`
  寫入點 = 每次 transition_with_certificate 結果（成功/失敗）後
- schema: `{requested_mode, effective_mode, live_order_allowed,
  audit_reasons: [str], updated_at}`
- dashboard-safe: audit_reasons 純字串（無 broker 敏感欄位）
- RED 測試: 寫入點存在 + **restart-read round-trip**（寫→重啟讀→
  effective_mode/audit_reasons 一致）

### 7.5 Release HEAD 檢查（修訂 §4）
- 檢查在 **actual release tree** 執行: `git -C <release_dir> rev-parse HEAD`
  （非 arbitrary cwd）== LRC_RELEASE_SHA → 否 → LIVE_QUARANTINED
  （RELEASE_IDENTITY_MISMATCH）
- acceptance: PM2 ecosystem.config.js env 注入 LRC_RELEASE_SHA 的驗證測試
  （wiring phase 交付）

### 7.6 Margin floor config（修訂 — 只測不設）
- 保守 TMF floor、**無 default**（missing key → ValueError —
  test_margin_source_malformed_config_fails 已蓋）
- value 選擇/owner = 獨立 approval — **本 phase 不加 config key**
- RED: futures.yaml 無 key（現況）→ wiring/config phase 才轉綠


## 8. v3 修訂（codex Phase-2 v2 review — 行為驗證取代文字/AST presence）

### 8.1 Per-route behavioural contract（recording-client，非 string）
| route | LIVE_QUARANTINED | LIVE_READY | 現況 |
|---|---|---|---|
| 2707 _place_safety_stop | **零 place** | 允許 | RED（無 gate） |
| 2721 _cancel_safety_stop | **零 cancel** | 允許 | RED（無 gate） |
| 3847 manager 路徑 | 零（_assert_live_allowed wrapper raise） | 允許 | GREEN（已 gate） |
| 4598 dispatcher | 零（gate raise） | 允許 | GREEN（已 gate） |
| 5208 _execute_trade | **零 place/cancel** | 允許 | RED（無 gate） |
AST inventory 保留為 completeness tripwire（test_exhaustive_state_changing_
routes_gated），**不是安全證明**。

### 8.2 Emergency 決策（_emergency_flatten_mts, monitor.py:7519 已實證）
現況: 設 `_mts_force_exit_inflight` → **改 strategy state**（_released_leg/
_side）→ 委派 normal dispatcher。**無 durable intent、無獨立授權檢查**。
決策: **quarantine 下允許，但需 4 項**（wiring phase 實作 + RED→GREEN）:
1. 獨立授權 operator command（manual close_all / settlement gate 攜帶
   durable authorization token）
2. **durable EXIT intent 先於任何 strategy mutation / broker I/O**
   （fsync'd intent ledger — core/emergency_intent）
3. idempotent（ledger dedup — 同 intent 重送 ≤1 fill request）
4. post-fill/restart reconciliation（outstanding intent 重啟後對帳）
若未授權 → 明確 blocked + operator procedure（不靜默）。

### 8.3 Reconnect atomic handoff（行為契約）
_try_shioaji_reconnect（main.py:693, 實證 body）: 現況 safe_login →
subscribe → return True — **不 quarantine、不 recertify**。
契約: ①re-login 前 fm ctx → LIVE_QUARANTINED ②resubscribe 完成後才
fresh certify ③login/subscribe/cert 任一失敗 → 維持 QUARANTINED。
manual + auto（code 12）同流程。RED 測試驅動真實 fn（monkeypatch
safe_login/connection_dropped）。

### 8.4 Dashboard persistence（core/execution_context_state）
- writer/reader 模組（wiring phase）: atomic write（tmp+rename+fsync,
  無 torn JSON）+ round-trip read
- RED: 模組不存在（ImportError）= 契約; wiring 後轉 GREEN

### 8.5 Release identity verifier（core/release_identity）
- verify_release_identity(release_dir, runner) — 指定 release_dir +
  注入 runner（可測）; env missing / HEAD mismatch / command failure
  全 fail-closed（QUARANTINED）
- RED: 模組不存在（ImportError）= 契約

### 8.6 Logout 集中化（移除 monitor-source 需求）
- 撤銷 v1 test_logout_invalidates_monitor_certificate_route 的
  monitor-source 斷言 — invalidation 正確集中於 shioaji_session.logout
- 契約: REAL logout → registry generation 死 → 綁定該 session 的 ctx
  無法達 LIVE_READY（core GREEN + 本檔案 behaviour 測試）

### 8.7 Exit sequence + no-orphan policy
- 正常 EXIT 順序（實證 5207→5208）: cancel safety stop → place exit
  （GREEN 契約鎖 — 順序不得反）
- quarantine 期間 outstanding exchange safety stop: **不 orphan** —
  經 emergency_intent ledger 留下 reconciliation 紀錄（RED→GREEN）;
  stop 本身為保護性 Cover 單，取消僅經 recovery


## 9. v3.1 修訂（codex Phase-2 v3 review — 7 點）

### 9.1 Emergency: 不另開帳本 — 用 P1-B core/exit_intent.py
- **本 wiring phase: LIVE_QUARANTINED 也擋 emergency**（無 bypass）—
  擋下時發 dashboard reason + operator procedure
- 未來獨立授權的 emergency operator command 必須走 **core/exit_intent.py**
  （canonical, P1-B）: IntentLog.create（durable + O_EXCL lock）→ child
  intent → emergency_supersede → client_order_id（pre-I/O idempotency）
  → recover（restart reconciliation）。平行帳本禁止（重新製造競態）。
- RED tests: ①quarantine 下 strategy 不得被 mutation ②blocked 必須發
  dashboard reason（EMERGENCY*）③future command 必須觸及 exit_intent
  ④protocol surface 鎖（create/submit_leg/emergency_supersede/
  reconciliation_view/recover/mark_terminal/client_order_id/
  SupersededIntentError）

### 9.2 ImportError tripwires 不計 GREEN
- contract_missing 依賴（core.execution_context_state /
  core.release_identity）單獨回報，不列入行為覆蓋數

### 9.3 Persistence: TRADING_RUNTIME_DIR（非裸 /tmp）
- 路徑 authority: {TRADING_RUNTIME_DIR}/execution_context.json —
  所有 release tree + dashboard 讀同一 canonical 檔
- 契約: atomic replace + fsync(file AND parent) / corrupt|missing →
  安全預設 LIVE_QUARANTINED / 無 broker|account 資料 / restart 後
  dashboard reader 正常 render

### 9.4 Release identity: 真實 git release dir
- 測試用真實 temp git repo + injected runner（env missing / HEAD
  mismatch / command failure → fail-closed）; PM2 ecosystem 注入 = 獨立
  integration RED（wiring phase 後續）

### 9.5 AST inventory 範圍擴大到 strategy package + adapters
- 實證新增 3 sites: shioaji_client.py:207 place / 215 update / 224 cancel
  （broker 直通點）→ **adapter 成為 chokepoint gate**（quarantine →
  零 place/cancel/update）; allowlist 7 sites; 新 site 出現 → tripwire fail

### 9.6 Reconnect ×3
- far resubscribe 失敗 / certification 失敗 / code-12 auto branch —
  各自: ctx 維持 QUARANTINED + 零 state-changing broker calls

### 9.7 Exit failure-side
- safety-stop cancel 失敗時 ordinary exit **不得靜默送單**（除非
  reconciliation policy 顯式允許 + durable reason 記錄）— RED

## 6. 交付順序

1. 本設計 + RED tests（commit）→ codex 審查 → 2. monitor wiring phase
（獨立授權）→ 3. GREEN → 4. review → 5. 獨立 deploy 決策。
