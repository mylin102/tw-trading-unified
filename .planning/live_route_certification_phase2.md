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

## 6. 交付順序

1. 本設計 + RED tests（commit）→ codex 審查 → 2. monitor wiring phase
（獨立授權）→ 3. GREEN → 4. review → 5. 獨立 deploy 決策。
