# Live Route Certification — 設計文件（design + RED tests only）

**狀態**: DESIGN（codex 審查中；本 phase 只交付設計 + RED tests，**不接線**）
**範圍**: `core/live_route_certificate.py`（新模組，proposal）+ `core/mode_transition.py` /
`core/live_broker_preflight.py` 的整合點。**不修改** monitor / order manager / production
routing；**不觸碰** authority projection 與 historical-audit 檔案。
**PAPER 必須維持現狀**：不切 config mode、不送/改/撤真單、不 deploy/restart/push。

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
    # trading_limits 不綁定：失敗僅為 auditable warning（§5）
```

- **short-lived**：`ttl_secs=60`（驗證時 `captured_at` 必須在 now 的
  `[now - ttl, now + skew_secs(30)]` 內；超過 → stale；未來 → clock skew）
- **process-bound**：驗證時 `process_start_id` 必須等於目前 process 的 start id；
  不同 process 產生的 cert 一律拒絕
- **session-bound**：cert 只能由「同一個已認證的執行 session」產生 —
  builder 必須接收 `api` 物件本身（或其 session token），**不得**由
  別的 process 拷貝的 dashboard JSON 直接組出（§4）

## 3. 必要唯讀查核（全數 fail-closed）

| # | 查核 | 失敗 → | 資料源 |
|---|---|---|---|
| 1 | authenticated account | QUARANTINE | api 登入 session（非 hasattr） |
| 2 | margin capacity（available margin 可讀且 >= 門檻） | QUARANTINE | api.margin(account) |
| 3 | broker flat（無持倉） | QUARANTINE | _safe_positions |
| 4 | no open orders | QUARANTINE | _safe_open_orders |
| 5 | 兩腿 distinct valid contracts（近/遠不同、delivery 有效） | QUARANTINE | resolve_near_far_contracts |
| 6 | snapshot code consistency（positions/orders 的 code 與 cert codes 一致） | QUARANTINE | 快照比對 |
| 7 | bidask subscribe 每腿 | QUARANTINE | safe_subscribe ×2 |
| 8 | bidask unsubscribe 每腿（完成後淨離開） | QUARANTINE | _unsubscribe_bidask ×2 |

全部通過 → `query_results` 記錄 8 項 → certificate 可核發。
任一失敗 → `certify_route()` 回傳 `(None, failures)` → 呼叫端進 LIVE_QUARANTINED。

## 4. Session 綁定（防跨 process 偽造）

- `build_live_broker_certificate(preflight: dict, api, process_start_id: str)`：
  `preflight` 必須由**同一 api 物件**當下呼叫 `collect_read_only_preflight(api)` 產出
  （builder 內部以 `api` 做一致性抽查：account hash 重算比對、process_start_id 相同）
- 不接受「外部 JSON 檔案」直接當輸入；preflight dict 只作為同一 session 的
  中間產物（process-bound 由 process_start_id + api session 雙重綁定）
- 測試：以「拷貝的 dashboard JSON」組 cert → 必須被拒（account hash 不符 /
  無 session 憑證）

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
- **PAPER requested mode 不得消費 cert**：`paper_context()` 行為不變；
  cert 只在 LIVE 路徑存在，PAPER 下 `assert_live_order_allowed()` 仍 raise
- 認證流程中**禁止任何 order/cancel/modify API 呼叫**（測試以 recording api 證明）

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
