# Live Route Certification — 設計文件 v5（design + RED tests only）

**狀態**: DESIGN v5（codex round-6 P0-auth/P0-margin + route 斷言修正；本 phase 只交付
capability map + 設計 + RED v5，**不接線**）
**範圍**: `core/live_route_certificate.py`（新模組，proposal）+ `core/mode_transition.py` /
`core/live_broker_preflight.py` 的整合點。**不修改** monitor / order manager / production
routing；**不觸碰** authority projection 與 historical-audit 檔案。
**PAPER 必須維持現狀**：不切 config mode、不送/改/撤真單、不 deploy/restart/push。
**v5 變更（round-6）**：P0-auth — auth adapter 改以 **Shioaji 1.7.0 實證 surface**
定義（見 `shioaji_capability_map.md`）：`futopt_account` 有效 **AND**
`list_accounts()` live 查詢非空 **AND** session epoch（系統 login wrapper 註冊）
一致；login_token/account/authenticated 等假想屬性**全部移除**。P0-margin —
`margin_rates` 證實不存在 → `margin_source_for` = **CONFIG_FLOOR only**
（reviewed pair-level floor，綁定 config commit/version；缺省/無效 fail-closed）；
account capacity 仍由 `api.margin(account)` 提供。新增 route 斷言：
issuer 綁定 canonical facts + **session epoch**；transition 拒收「同 nonce/issuer
但被竄改」的 cert（facts 由 **in-process runtime context** 重推導，非 cert/
caller 提供）；失敗 transition → **回傳顯式 LIVE_QUARANTINED ctx + audit reason**
（非僅 raise）；restart → 無任何 persisted snapshot/reconstructed issuer 可驗證
舊 cert；call-site 掃描改 **AST/import-aware**（斷言 monitor.py:522 已知呼叫移除）。
**v4 變更（B1-B6）**：B1 transition_with_certificate 在 redeem 前**原子地**驗證全部
fact（freshness/account/contracts/provider/session）；失敗 → 不達 LIVE_READY、
ctx 維持 non-ready、nonce **preserve**（不消費、不意外授權重試）；B2 關閉 legacy 旁路 —
`transition_to_live_ready(ctx, [])` 無 cert 必須 QUARANTINED（含 call-site 掃描測試）；
B3 margin 來源權威化 — `margin_source_for(api, product, config_floor)`：
broker margin query 優先、否則 reviewed config floor；finite positive 驗證、
product 適用性、source/value/version 綁定 cert；B4 auth adapter —
`is_authenticated_session(api)` 用實際 Shioaji session 訊號（strict allowlist，
無樂觀 fallback，exception → fail-closed）；B5 TOCTOU — validate 與 transition 之間
issuer invalidate/reconnect → 不得 LIVE_READY；成功 transition 恰好消費一次 nonce；
B6 persisted snapshot 僅 audit — 永不重建 issuer state；process restart 一律
PREFLIGHT/QUARANTINED 直到新 in-process cert。
**v3 變更（L1-L7）**：L1 驗證測試一律用發行 issuer + 先斷言 nonce redemption —
消除 NONCE_UNKNOWN 遮蔽；L2 open-orders 以顯式 Submitted/Pending 狀態建模；L3 確定性
required-margin provider（1 near + 1 far micro × buffer）綁定 cert 版本/值；L4 顯式
authenticated-session 斷言（Shioaji-appropriate，失敗 fail-closed）；L5 snapshot
presence 語意（空/缺/重複/subset 全 fail；market-closed 需顯式文件化例外）；L6 nonce
生命週期（peek/redeem/invalidate_all、reconnect 新 issuer、無序列化可還原授權）；
L7 真正的 transition 邊界（弱 legacy preflight 不得 LIVE_READY；唯一允許路徑 =
同 issuer 有效 cert 的 transition_with_certificate）。
**v2 變更（B1-B6）**：B1 fake 改為 adapter-faithful（走真實 collect_read_only_preflight
contract：futopt_account / Contracts.Futures.<sym> / list_positions / list_trades /
margin / trading_limits / snapshots / subscribe / unsubscribe）；B2 certify_route
**只能從目前 session 自行收集**，不接受任何外部 dict/JSON；B3 margin capacity = 明確
required margin（1 near + 1 far micro）+ 邊界測試；B4 subscribe↔unsubscribe 對稱、
unsubscribe 失敗 fatal、snapshot codes 綁定已解析合約；B5 弱 legacy preflight 不得
單獨達 LIVE_READY、PAPER 連有效 cert 都拒絕；B6 in-memory issuance nonce —
拷貝 JSON（即使 process_start_id 相同）無法通過驗證。

## 1. 目標

現行 startup preflight 是弱檢查（login flag / account hasattr / contract 物件存在）。
目標：以既有唯讀 preflight 能力（`collect_read_only_preflight`）產生**單一、短效、
process-bound 的 broker certificate**，作為未來 `LIVE_READY` 的**唯一**依據。
Certificate 不可變、可持久化；任何欄位缺失/查詢失敗/身份不符 → LIVE_QUARANTINED +
**零 live 送單**。

## 2. Certificate 規格（immutable, durable）

```python
@dataclass(frozen=True)
class LiveBrokerCertificate:
    version: int = 1
    nonce: str                   # in-memory 發行 handle（B6，不持久化）
    process_start_id: str            # 綁定產生 session（_generate_process_start_id）
    captured_at: str                 # ISO-8601（tz-aware）— 產生時刻
    account_hash: str                # _hash_account_id（不存明文帳號）
    near_code: str                   # 近月合約代碼（如 TMFH6）
    far_code: str                    # 遠月合約代碼
    position_snapshot_ts: str        # positions 快照時間（若有倉 → 不核發）
    order_snapshot_ts: str           # open orders 快照時間
    margin_available: float          # 可用保證金（缺失/不可讀 → fail）
    query_results: tuple[str, ...]   # 全部必要查核通過清單（見 §3）
    bidask_subscribed: tuple[str, ...]   # 雙腿 subscribe 成功
    bidask_unsubscribed: tuple[str, ...] # 雙腿 unsubscribe 成功
    warnings: tuple[str, ...] = ()   # trading_limits 等 warning-only 項目
    # trading_limits 不綁定：失敗僅為 auditable warning（§5）
```

- **short-lived**：`ttl_secs=60`（驗證時 `captured_at` 必須在 now 的
  `[now - ttl, now + skew_secs(30)]` 內；超過 → stale；未來 → clock skew）
- **process-bound**：驗證時 `process_start_id` 必須等於目前 process 的 start id；
  不同 process 產生的 cert 一律拒絕
- **session-bound（B6 核心）**：`CertificateIssuer`（process 內 in-memory registry）
  在發行時產生隨機 `nonce`（secrets.token_hex），cert 只存在於該 process 的
  issuer registry；**驗證要求 nonce 能在「目前 process 的 issuer」redeem** —
  拷貝的 JSON（即使 process_start_id 相同）nonce 不在 registry → NONCE_UNKNOWN
  拒絕。持久化的 preflight 記錄（broker_snapshot_*.json）**僅為 audit**，
  永不作為授權輸入

### CertificateIssuer（B6）
```python
class CertificateIssuer:
    """In-memory unforgeable issuance registry (per-process)."""
    _issued: dict[str, LiveBrokerCertificate]   # nonce → cert（僅記憶體）
    def issue(self, cert_without_nonce) -> LiveBrokerCertificate  # 產生 nonce + 註冊
    def peek(self, nonce) -> LiveBrokerCertificate | None         # 驗證用（不消費）
    def redeem(self, nonce) -> LiveBrokerCertificate | None       # transition 用（消費一次）
    def invalidate_all(self) -> None                              # session 續期/關閉
```

### nonce 生命週期（L6）
- `peek`：驗證用，不消費（cert 可多次驗證）
- `redeem`：transition 用，**消費一次**（single-use — 進入 LIVE_READY 即用掉）
- `invalidate_all`：session renewal / process shutdown 時清空 —
  之後任何舊 nonce 驗證 → NONCE_UNKNOWN
- reconnect → 新 `CertificateIssuer`（舊 cert 全部失效）
- **無序列化路徑可還原授權**：cert JSON 只是 audit 記錄；nonce 只存在於
  發行 process 的 issuer registry

### required-margin provider（L3，確定性、版本化）
```python
MARGIN_PROVIDER_VERSION = 1
def required_margin_for(product="TMF", *, per_lot_margin=100_000.0,
                        buffer=0.1) -> dict:
    """2 legs × per_lot_margin × (1 + buffer) — 確定性，可重算。"""
    # → {"provider_version": 1, "per_lot_margin": …, "buffer": …,
    #    "required_margin": round(2 * per_lot * (1 + buffer), 2)}
```
- cert 綁定 `margin_provider_version` + `required_margin`；
  validation 以**目前** provider 比對 — 版本/值改變 → PROVIDER_MISMATCH
- monitor wiring 不得接受任意 caller 輸入的 magic number — 一律走 provider

### margin 來源權威化（B3，取代 caller 輸入的 magic number）
```python
MARGIN_SOURCE_VERSION = 1
def margin_source_for(api, product="TMF", *, config_floor=100_000.0) -> dict:
    """per-pair margin 的權威來源（v5 — capability map 修正）：
    Shioaji 1.7.0 **無任何 per-contract margin rate 介面**（margin_rates 證實
    不存在）→ 唯一來源 = CONFIG_FLOOR（reviewed pair-level floor，
    config/futures.yaml 的明確參數），綁定 config commit SHA + version。
    驗證：finite positive；product ∈ 已知產品集；near/far 適用性。
    → {"source": "CONFIG_FLOOR", "version": 1, "config_commit": <sha>,
       "per_pair_margin": x, "product": product}
    config floor 缺省 / <=0 / NaN / 未知 product → raise（fail-closed）"""
```
- cert 綁定 `margin_source` + `margin_source_version` + `config_commit` +
  `required_margin`（= per_pair × (1+buffer)）；validation 以目前
  `margin_source_for()` 比對 — config commit / 值 / 版本改變 → SOURCE_MISMATCH
- **account capacity**（可用保證金）仍由 `api.margin(account)` 提供（preflight）
- 不得 call 任何未證實的 broker margin 介面（capability map §3）

### authenticated-session adapter（v5，Shioaji 實證 surface）
```python
def is_authenticated_session(api) -> bool:
    """Shioaji 1.7.0 實證 surface（strict，無樂觀 fallback）：
    (A) api.futopt_account 為有效 Account 物件
    (B) api.list_accounts() live 查詢回傳非空
    (C) session epoch 與系統 login wrapper 註冊值一致
    任一缺失/exception → False（fail-closed）。
    不得使用 login_token/account/authenticated（證實不存在）。"""
```
- session epoch：`core/shioaji_session.py` 的 `_login()` 成功後註冊
  `SESSION_EPOCH_BY_API[id(api)] = time.time()`（實作階段新增）；
  cert 綁定 epoch — reconnect → 新 epoch → 舊 cert 失效
- 登入證據 = (A)+(B)+(C) **AND** futopt_account 有效（單靠 account 物件
  存在不再是登入證明 — 正是要取代的弱條件）

### authenticated-session 斷言（L4）
- certify 前必須確認 api session 為已認證（Shioaji-appropriate：
  `api.authenticated is True` + `futopt_account` 存在），否則
  `AUTH_SESSION_UNAVAILABLE` fail-closed
- 不依賴 preflight dict 的 `authenticated: True` 自宣稱

## 3. 必要唯讀查核（全數 fail-closed）

| # | 查核 | 失敗 → | 資料源 |
|---|---|---|---|
| 1 | authenticated account | QUARANTINE | api 登入 session（非 hasattr） |
| 2 | margin capacity（available margin 可讀且 >= 門檻） | QUARANTINE | api.margin(account) |
| 3 | broker flat（無持倉） | QUARANTINE | _safe_positions |
| 4 | no open orders | QUARANTINE | _safe_open_orders |
| 5 | 兩腿 distinct valid contracts（近/遠不同、delivery 有效） | QUARANTINE | resolve_near_far_contracts |
| 6 | snapshot presence（**精確集合**：`set(codes) == {near_code, far_code}`；空/缺/重複/subset → fail；market-closed 需顯式文件化例外） | QUARANTINE | 快照比對（L5） |
| 7 | bidask subscribe 每腿 | QUARANTINE | safe_subscribe ×2 |
| 8 | bidask unsubscribe 每腿（**失敗 → fatal**，成功才記錄對稱） | QUARANTINE | _unsubscribe_bidask ×2 |

全部通過 → `query_results` 記錄 8 項 → `CertificateIssuer.issue()` 核發（含 nonce）。
任一失敗 → `certify_route()` 回傳 `(None, failures)` → 呼叫端進 LIVE_QUARANTINED。

## 4. Session 綁定（防跨 process 偽造）

- `certify_route(api, *, process_start_id, issuer, product, required_margin,
  margin_buffer)`：**只能從目前 session 收集** — 內部呼叫
  `collect_read_only_preflight(api, product)`；**不接受任何外部 dict/JSON 參數**
  （B2）— 拷貝/竄改的 dashboard JSON（含偽造 process_start_id）無法注入
- 認證全程零 order/cancel/modify API 呼叫（B6：fake 的 order 方法直接 raise）

### Margin capacity（B3）
- `required_margin` = 1 near + 1 far micro 的明確需求（呼叫端依 config/計算提供）
- capacity = `available_margin >= required_margin * (1 + margin_buffer)`
- 邊界測試：None → fail；0 → fail；just-below → fail；exactly-at → pass

## 5. trading_limits 政策

- `api.trading_limits(account)` 失敗 → **auditable warning only**
  （記錄於 cert 的 `warnings` / 診斷），不擋 certification
  （既有事實：部分 branch mapping 此 endpoint 不可用）
- **available margin 缺失/不可讀 → FAIL**（不可回退為 trading_limits 替代）

## 6. LIVE_READY 整合（設計，本 phase 不實作）

- `transition_to_live_ready(ctx, failures)` 的「成功路徑」改為要求
  `cert = certify_route(...)` 有效 + `validate_live_broker_certificate(cert)` 全過；
  缺 cert / 驗證失敗 → `ModeTransitionState.LIVE_QUARANTINED` +
  `assert_live_order_allowed()` raise
- **transition 邊界（L7）**：弱 legacy preflight（login flag/hasattr/contract 物件）
  **不得**單獨產生 LIVE_READY；唯一允許路徑 =
  `transition_with_certificate(ctx, cert, issuer)` — cert 必須由
  **同一個 in-process issuer** 發行（redeem 消費 nonce）
- **B2 legacy 旁路關閉**：`transition_to_live_ready(ctx, [])` 無 cert →
  **LIVE_QUARANTINED**（不再放行）；`with_effective_mode` 為 internal —
  任何 public route 不得暴露 LIVE_READY bypass；production call-site 掃描測試
- **B1/v5 原子驗證**：`transition_with_certificate` 在 redeem 前驗證全部
  fact（freshness/account/contracts/margin-source/session-epoch）— **facts 由
  in-process runtime context 重推導**（current account hash、near/far codes、
  margin source、session epoch），**非** cert/test/caller 提供；
  「同 nonce/issuer 但被竄改」的 cert → 拒收（非ce 只證明發行，facts 仍須
  與 runtime 一致）；任一失敗 → **回傳顯式 LIVE_QUARANTINED ctx + audit
  reason**（不單 raise）+ **nonce preserve**；成功才 redeem 消費一次 → LIVE_READY
- **B5 TOCTOU**：validate 與 transition 之間 issuer.invalidate_all()/
  reconnect → redeem 失敗 → 不得 LIVE_READY；成功 transition 恰好消費一次 —
  第二次 transition 同 cert → NONCE_UNKNOWN
- **restart（v5）**：無任何 persisted snapshot / reconstructed issuer 能
  驗證或 transition 舊 cert — 重啟後 issuer registry 為空 → 舊 nonce 全滅；
  唯一路徑 = 新 process 內重新 certify（fresh in-process cert）
- **call-site 掃描（v5）**：AST/import-aware — 解析 `strategies/futures/
  monitor.py` 的 AST，斷言 module 內無 `transition_to_live_ready` Call 節點
  （或精確斷言 monitor.py:522 已知呼叫已移除）；substring 掃描不足
- **PAPER requested mode 不得消費 cert**：`paper_context()` 行為不變；
  cert 只在 LIVE 路徑存在，PAPER 下 `assert_live_order_allowed()` 仍 raise
- 認證流程中**禁止任何 order/cancel/modify API 呼叫**（測試以 recording api 證明）

### 持久化與重啟（B6）
- `broker_snapshot_*.json` 等持久化記錄**僅為 audit** — 永不重建 issuer state、
  永不還原 nonce 授權
- **process restart 一律以 PREFLIGHT / LIVE_QUARANTINED 啟動**，直到
  新 process 內發行一份新鮮 cert

## 7. 驗證函式

```python
def validate_live_broker_certificate(cert, *, now_ts=None, process_start_id,
                                     account_hash, near_code, far_code,
                                     ttl_secs=60, skew_secs=30) -> tuple[bool, list[str]]:
    # stale / future clock skew / different process start /
    # different account / changed contract codes / missing field /
    # incomplete query_results → (False, reasons)
```

## 8. 測試（RED，本 phase 交付）

`tests/core/test_live_route_certificate.py`：
1. certificate 綁定 process_start_id / captured_at / account_hash / near+far codes /
   pos+order snapshot ts / margin / query_results / 雙腿 bidask 檢查
2. 8 項查核逐項 fail-closed（margin 缺、未認證、非 flat、有 open orders、
   合約不 distinct、code 不一致、subscribe 失敗、unsubscribe 失敗）
3. stale / future skew / 不同 process / 不同 account / 不同 contract codes → 拒絕
4. 跨 process 拷貝 JSON → 拒絕（session 綁定）
5. 查詢失敗 / 缺欄位 / 不完整 cert → LIVE_QUARANTINED + 零 live submit
6. PAPER 模式不能消費 cert
7. trading_limits 失敗 → warning only；margin 缺失 → fail
8. 認證全程零 order/cancel/modify API 呼叫（recording api）

## 9. 交付順序

1. 本設計 + RED tests（commit）→ codex 審查 → 2. 實作 `core/live_route_certificate.py`
   + 整合 → 3. GREEN → 4. review → 5. 獨立 deploy 決策（與 audit / authority 分離）。
