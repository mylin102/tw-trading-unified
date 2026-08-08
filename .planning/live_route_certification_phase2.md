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

## 6. 交付順序

1. 本設計 + RED tests（commit）→ codex 審查 → 2. monitor wiring phase
（獨立授權）→ 3. GREEN → 4. review → 5. 獨立 deploy 決策。
