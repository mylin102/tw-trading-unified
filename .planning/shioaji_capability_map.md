# Shioaji 1.7.0 Capability Map（Mini 實證，read-only）

**來源**: Mini `.venv` 安裝的 `shioaji==1.7.0`（rust core，`hello_from_bin`）；
`dir(sj.Shioaji)` + `inspect.signature` 實證（2026-08-08）。

## 1. Session / Login surface（已驗證存在）
| 成員 | 型別 | 語意 |
|---|---|---|
| `api.login(api_key, secret_key, subscribe_trade=True, receive_window=30000, force_refresh=False)` | method | **唯一** broker session 建立途徑（API-key 制；無 account/password 登入） |
| `api.futopt_account` | getset_descriptor | futures/options Account 物件（login 後由 SDK 設定） |
| `api.list_accounts()` | method | 列舉 session 綁定的帳戶（僅登入後可用） |
| `api.margin(account=None)` | method | 帳戶保證金查詢（僅登入後可用） |
| `api.account_balance(account=None)` | method | 帳戶餘額查詢 |
| `api.list_positions(account=None)` / `api.list_trades()` | method | 持倉 / 委託查詢（preflight 已用） |
| `api.logout()` | method | 登出 |

## 2. 已驗證**不存在**（先前設計的假想屬性 — 全部移除）
- `api.login_token` — **不存在**（dir(Shioaji) 無此名）
- `api.account`（證券商帳戶屬性）— **不存在**（僅 `futopt_account`）
- `api.authenticated` — **不存在**（自訂慣例，非 SDK surface）
- `api.margin_rates` — **不存在**（無 per-contract margin rate 查詢介面）
- 無 `UserLoginInfo` 型別；Account/Margin/TradingLimits 為 built-in（rust core）

## 3. Margin surface
- `api.margin(account)` → `Margin`（available_margin 等，rust builtin）—
  給 **account capacity**（可用保證金），非 per-contract rate
- **無任何 per-contract / per-pair initial margin 查詢** →
  per-pair floor 只能來自 **reviewed config**（CONFIG_FLOOR），
  缺省/無效 → fail-closed（不得 call 假想 broker 介面）

## 4. Auth adapter 推導（strict，無樂觀 fallback）
```
is_authenticated_session(api) :=
    futopt_account 為有效 Account 物件                     (A)
    AND list_accounts() 回傳非空（live session 查詢）       (B)
    AND 目前 session epoch（系統 login wrapper 註冊）       (C)
任何一步 exception/缺失 → False（fail-closed）
```
- session epoch：`core/shioaji_session.py` 在 `_login()` 成功後註冊
  `SESSION_EPOCH_BY_API[id(api)] = time.time()`（實作階段新增，設計先行）；
  reconnect → 新 epoch → 舊 cert 失效
- 登入證據 = (A)+(B)+(C) **AND** futopt_account 有效 —
  單靠「account 物件存在」不再被視為登入證明（正是要取代的弱條件）

## 5. 對測試 fake 的影響
- fake 移除 `login_token` / `account` / `authenticated` / `margin_rates`
- fake 實作真實 surface：`futopt_account` + `list_accounts()`（可設空/拋錯）+
  `margin(account)`（可設 None/拋錯）；session epoch 由測試註冊進 registry

## 6. Rust 物件限制（round-8 實證 — exact command/output）
```
$ .venv/bin/python3 -c "
import shioaji as sj, weakref
api = sj.Shioaji()
try:
    weakref.ref(api)
    print(\"weakref: OK\")
except TypeError as e:
    print(\"weakref: TypeError:\", e)
try:
    api._custom_attr = 123
    print(\"setattr: OK\")
except Exception as e:
    print(\"setattr:\", type(e).__name__, e)
"
type: <class 'builtins.Shioaji'>
weakref: TypeError: cannot create weak reference to 'builtins.Shioaji' object
setattr: AttributeError 'builtins.Shioaji' object has no attribute '_custom_attr'
```
- **weakref 不可用** → WeakKeyDictionary 設計不可實作（round-7 P0）—
  改 process-local **strong-registration map**（design v7 §6.2）
- **setattr 不可用** → 不得 monkey-patch api 物件；registry 一律 module-level
  （不得依賴 api 自訂屬性）
