# MTS 多投資標的切換設計：TMF / MTX

## 1. 文件目的

讓現有 MTS Calendar Spread 系統可透過設定檔中的 `ticker` 切換交易標的：

- `TMF`：微型臺指期貨
- `MTX`：小型臺指期貨

切換標的時，不修改策略程式碼、不建立新的 strategy plugin，並維持既有：

- Lifecycle state machine
- Entry / release / exit 決策語義
- Broker order flow
- Replay 與 research methodology

本次變更的核心不是單純替換 ticker，而是建立完整的 **product-aware runtime**。

---

## 2. 核心不變量

系統任何時刻都必須滿足：

```text
Config Product
= Product Spec
= Contract Pair Product
= Strategy Runtime Product
= Persistent State Product
= Order Intent Product
= Broker Contract Product
```

任一項不一致時，系統必須 **fail closed**，不得自動交易。

---

## 3. 初始資金是否應放大五倍

### 3.1 結論

若從 TMF 切換為 MTX，且仍維持：

- 相同交易口數
- 相同進出場點數
- 相同 stop / trail 點數
- 相同策略頻率
- 相同風險利用率

則 `initial_balance` 原則上應由 TMF 的基準資金放大約五倍。

原因是：

```text
TMF point_value = 10 TWD / point
MTX point_value = 50 TWD / point
```

同樣 1 口、同樣 10 點價格變動：

```text
TMF PnL = 10 × 10 = 100 TWD
MTX PnL = 10 × 50 = 500 TWD
```

MTX 的每點損益為 TMF 的五倍。若初始資金不變，則：

- 單筆損益占 equity 比率放大五倍
- Drawdown 比率放大五倍
- Margin utilization 顯著增加
- Risk of ruin 上升
- 原有回測中的風險比例失真

因此，若 MTS 原本 TMF 的：

```yaml
initial_balance: 100000
```

切換到 MTX 並維持一口交易時，可先使用：

```yaml
initial_balance: 500000
```

這是 **risk-equivalent capital scaling**，不是保證足夠的實際入金金額。

---

## 4. 不應直接固定乘五的情況

初始資金是否乘五，取決於切換後希望維持哪一個量：

| Risk policy | 保持不變 | MTX 初始資金 | 備註 |
|---|---|---:|---|
| `same_leverage` | 損益占 equity 比率 | 約 TMF × 5 | 建議作為預設 |
| `same_capital` | 帳戶資金 | 與 TMF 相同 | 風險比例放大約五倍 |
| `same_twd_risk` | 每筆最大 TWD 損失 | 可不乘五 | 必須縮小口數或縮短 stop |
| `same_contract_qty` | 交易口數 | 建議 × 5 | 每筆 TWD 風險同步放大 |
| `margin_based` | Margin utilization | 依實際 margin 計算 | 不應只看 point value |

對 MTS 現有架構，建議採用：

```text
capital_policy = same_leverage
```

因為這最能保持既有 TMF 策略在 MTX 上的相對風險結構。

---

## 5. 資金不得只由 point value 推導

`point_value × 5` 只能作為策略風險的第一階近似。實際最低資金還需要考慮：

- Broker 原始保證金
- 維持保證金
- 雙腿同時持倉的 margin requirement
- Calendar spread 是否有保證金減免
- 手續費與交易稅
- Slippage
- Release 後暫時變成單腿曝險
- 最大歷史 drawdown
- Broker 追繳與強制平倉邊界
- 夜盤流動性與 gap risk

因此 effective initial capital 建議取以下較大值：

```text
effective_initial_capital =
max(
    risk_equivalent_capital,
    margin_required_capital,
    drawdown_buffer_capital
)
```

其中：

```text
risk_equivalent_capital
= TMF baseline capital
  × target point value
  ÷ baseline point value
```

```text
margin_required_capital
= estimated peak margin
  ÷ max_margin_utilization
```

```text
drawdown_buffer_capital
= expected max drawdown
  ÷ max_drawdown_ratio
```

---

## 6. 建議的產品設定模型

### 6.1 Product specification

```yaml
ticker: TMF

products:
  TMF:
    contract_prefix: TMF
    point_value: 10
    tick_size: 1
    broker_fee_twd: 22
    margin_twd: 46000
    baseline_initial_capital_twd: 100000

  MTX:
    contract_prefix: MTX
    point_value: 50
    tick_size: 1
    broker_fee_twd: null
    margin_twd: null
    baseline_initial_capital_twd: 500000
```

`broker_fee_twd` 與 `margin_twd` 必須由目前 broker 或交易所資料注入，不應長期視為不可變常數。

---

## 7. 建議的 capital policy

```yaml
capital:
  policy: same_leverage
  baseline_product: TMF
  baseline_initial_capital_twd: 100000
  max_margin_utilization: 0.40
  max_drawdown_ratio: 0.20
```

可支援三種模式：

```yaml
capital:
  policy: same_leverage
```

```yaml
capital:
  policy: fixed
  initial_capital_twd: 300000
```

```yaml
capital:
  policy: margin_based
  max_margin_utilization: 0.40
```

---

## 8. Capital Resolver

建議新增純函式或 domain service：

```python
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class CapitalPolicy(str, Enum):
    SAME_LEVERAGE = "same_leverage"
    FIXED = "fixed"
    MARGIN_BASED = "margin_based"


@dataclass(frozen=True)
class ResolvedCapital:
    ticker: str
    policy: CapitalPolicy
    initial_capital_twd: Decimal
    risk_equivalent_capital_twd: Decimal
    margin_required_capital_twd: Decimal | None
    source: str
```

```python
def resolve_initial_capital(
    *,
    target_point_value: Decimal,
    baseline_point_value: Decimal,
    baseline_initial_capital_twd: Decimal,
    margin_twd: Decimal | None,
    peak_contracts: int,
    max_margin_utilization: Decimal,
) -> ResolvedCapital:
    risk_equivalent = (
        baseline_initial_capital_twd
        * target_point_value
        / baseline_point_value
    )

    margin_required = None
    if margin_twd is not None:
        margin_required = (
            margin_twd
            * peak_contracts
            / max_margin_utilization
        )

    effective = max(
        risk_equivalent,
        margin_required or Decimal("0"),
    )

    return ResolvedCapital(
        ticker="",
        policy=CapitalPolicy.SAME_LEVERAGE,
        initial_capital_twd=effective,
        risk_equivalent_capital_twd=risk_equivalent,
        margin_required_capital_twd=margin_required,
        source="product_config",
    )
```

---

## 9. Product Config Schema

### 9.1 統一設定

```yaml
ticker: MTX

products:
  TMF:
    point_value: 10
    tick_size: 1
    broker_fee_twd: 22
    margin_twd: 46000
    baseline_initial_capital_twd: 100000

  MTX:
    point_value: 50
    tick_size: 1
    broker_fee_twd: 35
    margin_twd: 120000
    baseline_initial_capital_twd: 500000

strategy_profiles:
  TMF:
    release_stop_pts: 10
    trail_distance_pts: 8
    max_quote_age_ms: 1000
    release_confirm_ticks: 2
    release_confirm_ms: 800

  MTX:
    release_stop_pts: 10
    trail_distance_pts: 8
    max_quote_age_ms: 1000
    release_confirm_ticks: 2
    release_confirm_ms: 800

capital:
  policy: same_leverage
  baseline_product: TMF
  baseline_initial_capital_twd: 100000
  max_margin_utilization: 0.40
```

### 9.2 Precedence

```text
Product registry defaults
    < session config
    < regime-specific strategy overrides
    < explicit runtime overrides
```

限制：

- Regime config 可以覆蓋 strategy parameter。
- Regime config 不可靜默覆蓋 `ticker`。
- Product identity 必須由頂層 runtime config 明確指定。
- Unknown ticker 必須 fail closed。
- MTX 缺少 margin 或 fee 時不得偷用 TMF 數值。

---

## 10. 參數單位治理

所有策略參數都必須使用明確單位後綴：

| Suffix | 意義 | 範例 |
|---|---|---|
| `*_pts` | 市場點數 | `release_stop_pts` |
| `*_twd` | 新臺幣金額 | `daily_loss_limit_twd` |
| `*_ticks` | 最小跳動數 | `confirm_ticks` |
| `*_ratio` | 無量綱比例 | `max_margin_utilization` |
| `*_ms` | 毫秒 | `confirm_ms` |
| `*_qty` | 口數 | `max_position_qty` |

禁止：

```yaml
stop_loss: 10
profit_lock: 500
threshold: 8
```

改為：

```yaml
release_stop_pts: 10
profit_lock_trigger_twd: 500
trail_distance_pts: 8
```

---

## 11. 點數參數不可自動乘五

以下參數通常代表市場價格距離，不因 MTX point value 較高而乘五：

- Spread entry threshold
- Z-score threshold
- ATR
- Bollinger Band distance
- VWAP distance
- Release stop points
- Trail distance points
- Break-even trigger points
- Tick confirmation count

例如：

```text
TMF release_stop_pts = 10
MTX release_stop_pts = 10
```

市場價格都移動十點，但 TWD 損益自然不同。

---

## 12. 金額參數必須 product-aware

以下參數若以 TWD 表示，必須納入：

```text
point_value × quantity
```

例如：

```python
pnl_twd = pnl_pts * point_value * quantity
```

```python
risk_ratio = abs(pnl_twd) / initial_capital_twd
```

```python
margin_utilization = required_margin_twd / current_equity_twd
```

必須避免：

```python
pnl_twd = pnl_pts * 10
```

或：

```python
initial_balance = 100000
```

散落在 strategy、monitor、dashboard 與 replay 中。

---

## 13. Contract Resolver

介面：

```python
get_near_far_contracts(
    product: str,
    *,
    trading_day: date,
) -> ContractPair
```

回傳：

```python
@dataclass(frozen=True)
class ContractPair:
    product: str
    near: ContractRef
    far: ContractRef
    resolved_for_trading_day: date
    source: str
```

必要 invariant：

```python
pair.product == requested_product
pair.near.product == requested_product
pair.far.product == requested_product
pair.near.expiry < pair.far.expiry
pair.near.code != pair.far.code
```

不得存在：

```python
product = "TMF"
```

的 resolver 內部預設。

---

## 14. Monitor 初始化流程

```text
Load runtime config
→ Resolve product spec
→ Resolve capital
→ Resolve trading day
→ Resolve near/far contracts
→ Validate product identity
→ Reconcile persistent state
→ Reconcile broker state
→ Subscribe ticks
→ Initialize strategy
→ Enable order submission
```

結構化 log：

```text
[MTS_PRODUCT_SELECTED]
ticker=MTX
point_value=50
tick_size=1
initial_capital_twd=500000
capital_policy=same_leverage
config_source=config/futures_night.yaml
```

```text
[MTS_CONTRACT_PAIR_RESOLVED]
ticker=MTX
near=MTXH6
far=MTXI6
trading_day=2026-...
source=shioaji
```

---

## 15. Persistent State 與 Export Namespace

所有 artifact 必須帶 product identity：

```text
exports/trades/TMF/orders.json
exports/trades/TMF/fills.json
exports/trades/TMF/trade_facts.parquet
exports/trades/TMF/trade_snapshots.parquet

exports/trades/MTX/orders.json
exports/trades/MTX/fills.json
exports/trades/MTX/trade_facts.parquet
exports/trades/MTX/trade_snapshots.parquet
```

State payload：

```json
{
  "schema_version": 2,
  "ticker": "MTX",
  "point_value": 50,
  "initial_capital_twd": 500000,
  "near_contract": "MTXH6",
  "far_contract": "MTXI6",
  "lifecycle_state": "ARMED"
}
```

不得在 ticker 切換後載入另一產品 state。

---

## 16. Cross-Product Restart Gate

以下情況必須拒絕啟動：

```text
config ticker = MTX
state ticker = TMF
```

```text
config ticker = MTX
broker position = TMF
```

```text
config ticker = MTX
pending order contract = TMF
```

錯誤碼：

```text
MTS_PRODUCT_IDENTITY_MISMATCH
MTS_BROKER_POSITION_PRODUCT_MISMATCH
MTS_OPEN_ORDER_PRODUCT_MISMATCH
MTS_ARTIFACT_PRODUCT_MISMATCH
```

不得以清空 state、fallback 到 FLAT 或忽略舊持倉的方式恢復。

---

## 17. 切換標的操作流程

### 17.1 TMF → MTX

```text
1. Block new TMF entries
2. Drain current lifecycle to FLAT
3. Confirm local pending orders = 0
4. Capture broker snapshot
5. Confirm TMF broker position = 0
6. Confirm TMF broker open orders = 0
7. Persist switch checkpoint
8. Change config ticker to MTX
9. Resolve MTX product spec
10. Resolve MTX initial capital
11. Resolve MTX near/far contracts
12. Start in paper or shadow mode
13. Validate tick subscription and PnL conversion
14. Complete broker preflight
15. Explicitly activate order submission
```

禁止直接修改：

```yaml
ticker: MTX
```

後立刻 PM2 restart 並允許實單。

---

## 18. Dashboard 與 Equity

Dashboard 必須從 runtime product spec 讀取：

```python
equity_twd = (
    initial_capital_twd
    + cumulative_realized_pnl_twd
    + unrealized_pnl_twd
)
```

顯示內容至少包括：

```text
Ticker
Point value
Initial capital
Current equity
Required margin
Margin utilization
Realized PnL
Unrealized PnL
PnL percentage
Capital policy
```

PnL percentage：

```python
pnl_pct = total_pnl_twd / initial_capital_twd
```

如此 TMF 與 MTX 才能在相同風險尺度上比較。

---

## 19. Replay 與 Research Provenance

每個 replay case 必須保存：

```json
{
  "ticker": "MTX",
  "point_value": 50,
  "quantity": 1,
  "initial_capital_twd": 500000,
  "capital_policy": "same_leverage",
  "product_config_hash": "...",
  "strategy_profile_hash": "..."
}
```

Replay 不可從目前 config 猜測歷史交易使用的 product economics。

---

## 20. 測試矩陣

| Test layer | TMF | MTX | Cross-product negative |
|---|---:|---:|---:|
| Config loading | ✓ | ✓ | ✓ |
| Product spec | ✓ | ✓ | ✓ |
| Capital resolver | ✓ | ✓ | N/A |
| Contract resolver | ✓ | ✓ | ✓ |
| Monitor initialization | ✓ | ✓ | ✓ |
| Tick subscription | ✓ | ✓ | ✓ |
| PnL conversion | ✓ | ✓ | N/A |
| Equity calculation | ✓ | ✓ | N/A |
| Artifact paths | ✓ | ✓ | ✓ |
| State recovery | ✓ | ✓ | ✓ |
| Broker preflight | ✓ | ✓ | ✓ |
| Order intent validation | ✓ | ✓ | ✓ |
| Replay determinism | ✓ | ✓ | N/A |

---

## 21. 必要測試案例

### 21.1 Product config

```python
def test_tmf_product_spec():
    assert config.product.ticker == "TMF"
    assert config.product.point_value == Decimal("10")


def test_mtx_product_spec():
    assert config.product.ticker == "MTX"
    assert config.product.point_value == Decimal("50")


def test_unknown_ticker_fails_closed():
    with pytest.raises(UnknownProductError):
        load_product_spec("UNKNOWN")
```

### 21.2 Capital scaling

```python
def test_same_leverage_scales_mtx_capital_five_times():
    resolved = resolve_initial_capital(
        target_point_value=Decimal("50"),
        baseline_point_value=Decimal("10"),
        baseline_initial_capital_twd=Decimal("100000"),
        margin_twd=None,
        peak_contracts=2,
        max_margin_utilization=Decimal("0.40"),
    )

    assert resolved.initial_capital_twd == Decimal("500000")
```

### 21.3 PnL conversion

```python
def test_same_points_have_different_twd_pnl():
    assert points_to_twd(
        Decimal("10"),
        point_value=Decimal("10"),
        quantity=1,
    ) == Decimal("100")

    assert points_to_twd(
        Decimal("10"),
        point_value=Decimal("50"),
        quantity=1,
    ) == Decimal("500")
```

### 21.4 Product mismatch

```python
def test_mtx_runtime_rejects_tmf_state():
    with pytest.raises(ProductIdentityMismatch):
        reconcile(
            runtime_product="MTX",
            persisted_product="TMF",
        )
```

---

## 22. 建議 PR 拆分 + Task Breakdown

### PR 1 — Product Domain Model + Config

**Design:**
- `FuturesProductSpec`
- Product registry
- Config precedence
- Unknown ticker fail closed
- TMF backward compatibility

**Tasks:**
- [ ] Define `FuturesProductSpec` dataclass (ticker, point_value, tick_size, broker_fee_twd, margin_twd, baseline_initial_capital_twd)
- [ ] Build product registry with TMF / MTX entries
- [ ] Amend futures.yaml schema: add `products:` block, `capital:` block, `strategy_profiles:` block
- [ ] Config loader: ticker → product spec resolution
- [ ] Unknown ticker → `UnknownProductError` (fail closed)
- [ ] Legacy config compat: TMF-only config still loads correctly
- [ ] `test_tmf_product_spec()` / `test_mtx_product_spec()` / `test_unknown_ticker_fails_closed()`

**Files likely touched:**
- `core/product_spec.py` (new)
- `core/config_loader.py` (modify)
- `config/futures.yaml` (modify)
- `config/futures_night.yaml` (modify)
- `tests/test_product_config.py` (new)

---

### PR 2 — Capital Model + Unit Governance

**Design:**
- `CapitalPolicy`
- `ResolvedCapital`
- Same-leverage scaling
- Margin utilization
- Points / TWD conversion
- Equity calculation

**Tasks:**
- [ ] Define `CapitalPolicy` enum (SAME_LEVERAGE / FIXED / MARGIN_BASED)
- [ ] Define `ResolvedCapital` dataclass
- [ ] Implement `resolve_initial_capital()` pure function
- [ ] Points ↔ TWD conversion helpers (`points_to_twd()`, `twd_to_points()`)
- [ ] Equity calculation: `initial_capital + realized_pnl + unrealized_pnl`
- [ ] Replace all hardcoded `initial_balance` / `initial_capital` references
- [ ] `test_same_leverage_scales_mtx_capital_five_times()`
- [ ] `test_same_points_have_different_twd_pnl()`

**Files likely touched:**
- `core/capital_resolver.py` (new)
- `core/product_spec.py` (append)
- `strategies/futures/monitor.py` (modify)
- `tests/test_capital_resolver.py` (new)

**Scaling rules:**
- Point parameters (`*_pts`) → NOT scaled by point_value
- TWD parameters (`*_twd`) → scaled by point_value × quantity
- `same_leverage` capital → scaled by point_value ratio
- Margin → actual broker/clearing values, not derived

---

### PR 3 — Product-Aware Contract Resolver

**Design:**
- Generic TMF / MTX resolver
- Safe Mode product filtering
- Rolling logic
- Deterministic resolver tests

**Tasks:**
- [ ] `get_near_far_contracts(product: str, ...)` → `ContractPair` (product-agnostic)
- [ ] `ContractPair` with invariants: `pair.near.product == requested_product`
- [ ] Safe Mode filtering for any product code
- [ ] Rolling contract exclusion for any product
- [ ] `test_tmf_contract_pair()` / `test_mtx_contract_pair()`
- [ ] `test_cross_product_contract_pair_rejected()`

**Files likely touched:**
- `core/contract_resolver.py` (refactor)
- `core/models.py` or new `core/contract_pair.py` (new dataclass)
- `tests/test_contract_resolver.py` (new)

---

### PR 4 — Monitor Wiring

**Design:**
- Dynamic ticker
- Product-bound contract pair
- Tick subscription
- Structured logs
- Mock integration tests

**Tasks:**
- [ ] `FuturesMonitor.__init__` loads product spec from config ticker
- [ ] `_init_contracts()` / `_resolve_contract_pair()` use `self.product`
- [ ] Tick subscription uses resolved near/far contract codes
- [ ] Structured init logs: `[MTS_PRODUCT_SELECTED]`, `[MTS_CONTRACT_PAIR_RESOLVED]`
- [ ] `test_monitor_init_with_tmf()` / `test_monitor_init_with_mtx()`
- [ ] `test_monitor_init_rejects_unknown_ticker()`

**Files likely touched:**
- `strategies/futures/monitor.py` (modify)
- `core/contract_resolver.py` (minor)
- `tests/test_monitor_init.py` (new)

---

### PR 5 — Artifact Namespace + Recovery

**Design:**
- Product-specific paths
- State schema v2
- Cross-product restart gate
- Legacy TMF migration

**Tasks:**
- [ ] Export paths: `exports/trades/{TICKER}/orders.json` etc.
- [ ] State file includes ticker, point_value, initial_capital_twd
- [ ] Cross-product restart detection: config vs state ticker mismatch
- [ ] Mismatch → `ProductIdentityMismatch` error (fail closed)
- [ ] Legacy `TMF_` files unchanged (readable but write to new paths)
- [ ] `test_cross_product_state_mismatch_fails_closed()`

**Files likely touched:**
- `core/order_management/order_manager.py` (export path)
- `core/state_manager.py` or monitor state persistence
- `strategies/plugins/futures/active/tmf_spread.py` (state)
- `tests/test_state_recovery.py` (new)

---

### PR 6 — MTX Paper Enablement

**Design:**
- MTX config
- Read-only Shioaji contract probe
- Paper / shadow validation
- No live activation

**Tasks:**
- [ ] Add MTX config profile (futures_mtx.yaml or products.MTX in existing)
- [ ] Validate MTX contracts resolvable via Shioaji
- [ ] Paper run: tick ingestion, spread calculation, lifecycle flow
- [ ] No live order submission until explicitly activated
- [ ] Validate PnL conversion for MTX point_value
- [ ] Document MTX initial capital recommendation

**Files likely touched:**
- `config/futures_mtx.yaml` (new)
- `core/product_spec.py` (ensure MTX entry populated)
- `docs/mts-product-switch-tmf-mtx.md` (update)  
- `tests/test_mtx_paper_validation.py` (new)

---

## 23. Acceptance Criteria

### Product selection

- [ ] `ticker: TMF` 載入 TMF product spec。
- [ ] `ticker: MTX` 載入 MTX product spec。
- [ ] Unknown ticker 拒絕啟動。
- [ ] Regime config 不可覆蓋 product identity。

### Capital

- [ ] `same_leverage` 下，TMF 100,000 對應 MTX 500,000。
- [ ] Effective capital 同時受 margin floor 約束。
- [ ] Dashboard、risk engine、replay 使用同一 resolved capital。
- [ ] 不存在散落的固定 `initial_balance=100000`。

### Strategy

- [ ] 點數參數不因 point value 自動乘五。
- [ ] 金額參數使用 point value 與 quantity 換算。
- [ ] Lifecycle、entry、release、exit 語義不變。
- [ ] ADR-011 phase isolation regression 全數通過。

### Contract

- [ ] TMF 回傳 TMF near/far contracts。
- [ ] MTX 回傳 MTX near/far contracts。
- [ ] 不允許 cross-product contract pair。
- [ ] Safe Mode 為 requested-product closed set。

### Persistence

- [ ] State、orders、fills、datasets 帶 ticker。
- [ ] TMF 與 MTX artifacts 不混用。
- [ ] Cross-product restart fail closed。
- [ ] Legacy TMF data 不被覆寫。

### Operational safety

- [ ] 切換前 local lifecycle 為 FLAT。
- [ ] 舊產品 broker position 為 0。
- [ ] 舊產品 broker open orders 為 0。
- [ ] MTX 先經 paper / shadow 驗證。
- [ ] Live activation 需通過既有 broker preflight 與 atomic commit。

---

## 24. ADR 建議

```text
ADR-020: Product-Aware Futures Strategy Runtime and Capital Scaling
```

### Decision

1. `ticker` 是 product identity 的唯一入口。
2. Product spec 是 point value、tick size、fee、margin 的唯一來源。
3. Strategy price-distance parameters 保持 points 語義。
4. Monetary risk 使用 product economics 換算。
5. 預設 capital policy 為 `same_leverage`。
6. TMF 100,000 baseline 對應 MTX 500,000 risk-equivalent capital。
7. Effective capital 必須再通過 margin 與 drawdown floor。
8. 所有 persistent artifacts 必須綁定 ticker。
9. Cross-product state、position 或 order mismatch 一律 fail closed。
10. Lifecycle FSM 與 broker order flow 不因產品切換而改變。

### Consequences

正面：

- TMF / MTX 可由 config 安全切換。
- 維持跨產品相對一致的 leverage 與 drawdown 比率。
- 避免點數風險與 TWD 風險混淆。
- 降低 cross-product state split-brain。
- 未來可延伸其他同型 futures product。

代價：

- Config schema 需要升版。
- State 與 research metadata 需要 migration。
- 現有硬編碼 `TMF_` 與 `initial_balance` 必須集中治理。
- MTX 啟用前需完成獨立 paper calibration。

---

## 25. 最終建議

對目前 MTS：

```text
TMF baseline initial capital = 100,000 TWD
MTX same-leverage initial capital = 500,000 TWD
```

但正式啟用 MTX 前，應以以下公式確認最終數值：

```text
MTX effective initial capital
=
max(
    500,000,
    peak margin requirement / allowed margin utilization,
    expected max drawdown / allowed drawdown ratio
)
```

因此 `500,000 TWD` 應被視為 **risk-equivalent baseline**，不是無條件的最終最低入金額。

在架構上，不應直接寫：

```yaml
initial_balance: 500000
```

而應由：

```text
ticker
→ product spec
→ capital policy
→ resolved initial capital
```

統一產生，並將 resolved value 寫入 runtime provenance、state、dashboard 與 replay metadata。
