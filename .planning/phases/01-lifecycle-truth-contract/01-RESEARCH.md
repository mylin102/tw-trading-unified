# Phase 1: Lifecycle Truth Contract - Research

**Researched:** 2026-04-20  
**Domain:** Futures/options execution lifecycle contract, broker/deal truth, position mutation safety  
**Confidence:** MEDIUM

## User Constraints

- Taiwan futures/options trading system; bugs can cause real financial loss. [VERIFIED: user prompt]
- Keep `PaperTrader.position` as the position truth anchor. [VERIFIED: user prompt]
- Side effects only after successful operations. [VERIFIED: user prompt]
- Position and cost basis must not update from intent/order placement. [VERIFIED: user prompt]
- Preserve current `main.py` and 8500 dashboard behavior. [VERIFIED: user prompt]
- This phase is contract/model work; do not scope in Rust/GCP/strategy changes. [VERIFIED: user prompt]

## Phase Requirements

| ID | Description | Research Support |
|---|---|---|
| EXEC-01 | Operator can trace every futures/options, paper/live trade through linked intent, order, and deal records | Add explicit `intent_id`, `order_id`, `deal_id`, plus broker IDs (`order.id`/`seqno`/`ordno`/`trade_id`/`exchange_seq`) and keep export/read-model compatibility. [VERIFIED: .planning/REQUIREMENTS.md:10-12][VERIFIED: docs/example_order_manager.py:49-97] |
| EXEC-02 | Distinguish accepted, partial fill, full fill, cancel, reject states | Separate order-state transitions from deal accumulation; do not treat fill as the only lifecycle channel. [VERIFIED: .planning/REQUIREMENTS.md:10-12][CITED: docs/shioaji_訂單生命週期.md] |
| EXEC-03 | Position and cost basis update only from confirmed deal data | Route all position/cost-basis mutation through confirmed-deal handlers; do not mutate on submit/intent. [VERIFIED: .planning/REQUIREMENTS.md:10-12][VERIFIED: strategies/futures/monitor.py:1404-1519] |

## Summary

The biggest planning fact is that lifecycle truth is currently split three ways: broker/order submission state, deal/fill state, and position/log/dashboard state are updated by different code paths with different identifiers. [VERIFIED: strategies/futures/monitor.py:1404-1519][VERIFIED: strategies/options/live_options_squeeze_monitor.py:1212-1278][VERIFIED: core/dashboard_positions.py:53-118] Futures live is the highest-risk path because it can place a live order and still call `PaperTrader.execute_signal(...)` in the same flow, which means position/cost logic can move before broker-confirmed fill truth exists. [VERIFIED: strategies/futures/monitor.py:1404-1519] Options live is safer on fill mutation because it updates position on deal callback, but it ignores order-status callbacks entirely, so accepted/cancel/reject states are not preserved as first-class lifecycle truth. [VERIFIED: strategies/options/live_options_squeeze_monitor.py:1212-1219]

The best reuse path already exists in-repo: `core.order_management` provides an exportable order model, dashboard panels already consume `*_orders.json`, and `tests/test_order_lifecycle/` already cover partial fill, cancel, reject, recovery, and dashboard export behavior. [VERIFIED: core/order_management/order.py:13-260][VERIFIED: ui/dashboard.py:1489-1616][VERIFIED: ui/dashboard.py:1768-1901][VERIFIED: tests/test_order_lifecycle/test_order_manager.py:1-372] But the current shared model is still too shallow for Phase 1 because it has no `intent_id`, no durable deal records, and only one broker ID field (`exchange_order_id`). [VERIFIED: core/order_management/order.py:101-109][VERIFIED: core/order_management/order_manager.py:96-168]

**Primary recommendation:** Extend the existing shared order model into an explicit `Intent -> Order -> Deal` contract, and make confirmed-deal application the only place allowed to mutate position and cost basis. [VERIFIED: docs/example_order_manager.py:49-97][CITED: docs/shioaji_訂單生命週期.md]

## Project Constraints (from RULES.md)

- Run `python3 -m pytest tests/ -v` before and after changes. [VERIFIED: RULES.md:5-7]
- Side effects such as CSV/log/notification must happen only after core operation succeeds. [VERIFIED: RULES.md:10-18]
- `PaperTrader.position` is the only futures position truth. [VERIFIED: RULES.md:20-25]
- Ledger CSV is a log, not a state store. [VERIFIED: RULES.md:20-25]
- Guard entry/exit before mutation; on exit, zero state before logging. [VERIFIED: RULES.md:27-43]
- PnL must include all fees/costs. [VERIFIED: RULES.md:45-52]
- Preserve hot-reload/runtime conventions; `main.py` remains startup/orchestration entry. [VERIFIED: RULES.md:94-107][VERIFIED: RULES.md:111-132]

## Standard Stack

### Core

| Library / Module | Version | Purpose | Why Standard | Source |
|---|---:|---|---|---|
| Python dataclasses / enums / typing | stdlib | Contract types and state enums | Existing example and shared manager already use this style. | [VERIFIED: docs/example_order_manager.py:1-120][VERIFIED: core/order_management/order.py:6-39] |
| `core.order_management` | in-repo | Shared order model/export surface | Already wired into futures, options, tests, and dashboard export. | [VERIFIED: strategies/futures/monitor.py:181-195][VERIFIED: strategies/options/live_options_squeeze_monitor.py:3008-3018] |
| Shioaji | 1.3.3 | Broker trade/order source | Existing runtime already depends on it for live state. | [VERIFIED: local environment][VERIFIED: requirements.txt:1][CITED: docs/shioaji_訂單生命週期.md] |
| pandas | 2.3.3 | Dashboard/recovery read models | Existing dashboard and audit/rebuild paths rely on it. | [VERIFIED: local environment][VERIFIED: core/dashboard_positions.py:1-170][VERIFIED: core/order_lifecycle_audit.py:1-106] |

### Supporting

| Library / Module | Version | Purpose | When to Use | Source |
|---|---:|---|---|---|
| pytest | 9.0.2 | Regression lock for lifecycle contract | Use for contract, recovery, and dashboard export tests. | [VERIFIED: local environment][VERIFIED: pytest.ini:1-4] |
| `core.order_lifecycle_audit` | in-repo | Rebuild/read-model repair for options orders file | Use only as repair/read model, not truth source. | [VERIFIED: core/order_lifecycle_audit.py:12-106] |
| `ui/dashboard.py` orders panels | in-repo | Compatibility target for `*_orders.json` shape | Preserve current fields and status strings. | [VERIFIED: ui/dashboard.py:1489-1616][VERIFIED: ui/dashboard.py:1768-1901] |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|---|---|---|
| Extending `core.order_management` | Reusing `strategies/options/options_engine/engine/order_manager.py` | That options-specific manager is duplicate/parallel design and is not the shared path currently used by futures/options monitors. [VERIFIED: strategies/options/options_engine/engine/order_manager.py:1-115][VERIFIED: strategies/options/live_options_squeeze_monitor.py:3008-3018] |
| Ledger/CSV-derived truth | Broker/deal-driven contract + exported read models | Ledger/CSV rebuild paths use synthetic IDs and flatten entry/exit semantics, so they are suitable for recovery/display helpers, not lifecycle truth. [VERIFIED: strategies/futures/monitor.py:1199-1270][VERIFIED: strategies/options/live_options_squeeze_monitor.py:2520-2606] |

## Architecture Patterns

### Recommended Project Structure

```text
core/
├── execution_contract/      # intent/order/deal dataclasses + enums
├── order_management/        # existing shared order export/read model
└── order_lifecycle_audit.py # repair/read-model only

strategies/
├── futures/monitor.py       # adapt submit -> lifecycle contract -> PaperTrader apply-on-deal
└── options/live_options_squeeze_monitor.py # adapt callbacks to same contract

tests/test_order_lifecycle/
├── test_contract_traceability.py
├── test_order_state_vs_deal_state.py
└── test_position_apply_on_confirmed_deal.py
```

[VERIFIED: current codebase structure]

### Pattern 1: Separate order-state and deal-state channels

**What:** Model order acceptance/cancel/reject as one stream and fills/deals as another; aggregate them into one lifecycle view without collapsing them. [CITED: docs/shioaji_訂單生命週期.md]  
**When to use:** Everywhere, especially live Shioaji paths. [CITED: docs/shioaji_訂單生命週期.md]

### Pattern 2: Local IDs first, broker IDs attached later

**What:** Mint a local `intent_id` before submit, a local `order_id` when submit is attempted, and local `deal_id` per confirmed deal; attach broker keys when they appear. [VERIFIED: docs/example_order_manager.py:49-97]  
**When to use:** All paper/live futures/options flows. [VERIFIED: docs/example_order_manager.py:49-97]

**Safest identifier set:**
- `intent_id` — local strategy intent key. [VERIFIED: docs/example_order_manager.py:49-60]
- `order_id` — local lifecycle order key. [VERIFIED: docs/example_order_manager.py:75-98]
- `broker_order_id` (`order.id`) — broker order identity. [CITED: docs/shioaji_訂單生命週期.md]
- `seqno`, `ordno` — broker/exchange order tracking. [CITED: docs/shioaji_訂單生命週期.md]
- `deal_id` — local confirmed-deal key. [ASSUMED]
- `trade_id`, `exchange_seq` — broker/exchange deal tracking. [CITED: docs/shioaji_訂單生命週期.md]

### Pattern 3: Apply position/cost via a single deal applier

**What:** One function consumes confirmed deal records and mutates position, entry price / cost basis, realized PnL, and pending remaining quantity. [VERIFIED: strategies/options/live_options_squeeze_monitor.py:1225-1278][VERIFIED: strategies/futures/squeeze_futures/engine/simulator.py:286-320]  
**When to use:** For futures `PaperTrader.position` and for any options position anchor. [VERIFIED: RULES.md:20-25]

### Anti-Patterns to Avoid

- **Optimistic position mutation after `place_order()`:** Futures live currently places the order and still runs `PaperTrader.execute_signal(...)` in the same path. [VERIFIED: strategies/futures/monitor.py:1404-1519]
- **Treating fills as the only lifecycle event:** Options live currently ignores non-deal order callbacks, so accepted/cancel/reject truth is lost. [VERIFIED: strategies/options/live_options_squeeze_monitor.py:1212-1219]
- **Using ledger/orders JSON as state truth:** Current dashboard/recovery helpers reconstruct state from logs/files with synthetic IDs. [VERIFIED: core/dashboard_positions.py:53-118][VERIFIED: core/order_lifecycle_audit.py:34-91]
- **Handling partial fill only at terminal fill:** Futures callback bridge only updates `PaperTrader` when event status is `FILLED`, not on partial confirmed deals. [VERIFIED: strategies/futures/monitor.py:1279-1307]

## Don’t Hand-Roll

| Problem | Don’t Build | Use Instead | Why |
|---|---|---|---|
| Broker lifecycle truth | Single flat “trade row” model | `Intent -> Order -> Deal` contract | Flat rows lose accepted/partial/cancel/reject semantics. [CITED: docs/shioaji_訂單生命週期.md] |
| Display compatibility | New dashboard schema | Existing `Order.to_dict()`/`*_orders.json` surface | Current dashboard already reads these files. [VERIFIED: ui/dashboard.py:1489-1616][VERIFIED: ui/dashboard.py:1768-1901] |
| Recovery truth | Timestamp-based synthetic IDs as primary keys | Local IDs + broker IDs + recovery hydration | Current recovery helpers synthesize IDs from timestamps/ledger rows. [VERIFIED: strategies/futures/monitor.py:1249-1258][VERIFIED: strategies/options/live_options_squeeze_monitor.py:2578-2596] |
| State polling logic | Callback-only truth | Callback + `update_status()` reconciliation design | Shioaji docs explicitly require refresh/reconcile behavior. [CITED: docs/shioaji_訂單生命週期.md] |

**Key insight:** Phase 1 should not invent a new engine; it should formalize the contract around the code that already exists and remove the unsafe mutation points. [VERIFIED: codebase review]

## Common Pitfalls

### Pitfall 1: Futures live mutates position on submit path

**What goes wrong:** The broker order can be accepted/rejected/partial later, but local position/cost logic already moved. [VERIFIED: strategies/futures/monitor.py:1404-1519]  
**How to avoid:** Split “submit accepted” from “deal applied”; only the latter may touch `PaperTrader.position`. [VERIFIED: RULES.md:10-25]

### Pitfall 2: Options live drops order-state truth

**What goes wrong:** Accepted/cancel/reject states are invisible because only deal callbacks are processed. [VERIFIED: strategies/options/live_options_squeeze_monitor.py:1212-1219]  
**How to avoid:** Preserve order events into the shared contract even if position ignores them. [CITED: docs/shioaji_訂單生命週期.md]

### Pitfall 3: Partial fills are under-modeled

**What goes wrong:** Shared manager can represent partial fills, but futures callback bridge only applies terminal fill to `PaperTrader`; shallow planning will miss incremental position/cost updates. [VERIFIED: core/order_management/order_manager.py:138-175][VERIFIED: strategies/futures/monitor.py:1279-1307]  
**How to avoid:** Add deal-applier semantics for partials and remaining quantity.

### Pitfall 4: Recovery and dashboard files look authoritative but are read models

**What goes wrong:** Orders JSON and ledger rebuild logic can make history appear complete even when broker IDs and deal links are missing. [VERIFIED: core/order_lifecycle_audit.py:34-91][VERIFIED: core/dashboard_positions.py:53-118]  
**How to avoid:** Keep export files backward-compatible, but derive them from lifecycle truth, not vice versa.

### Pitfall 5: Current code has latent API mismatches

**What goes wrong:** Futures monitor calls `get_pending_orders()` and `cancel_order()` on `self.order_mgr`, but the shared manager exposes `get_pending()` and `cancel()`. [VERIFIED: strategies/futures/monitor.py:1918-1924][VERIFIED: core/order_management/order_manager.py:178-219][VERIFIED: core/order_management/order_manager.py:321-326]  
**How to avoid:** Plan contract work with explicit adapter boundaries; do not assume current shared manager APIs are consistently consumed.

## Code Examples

### Contract skeleton

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class OrderState(str, Enum):
    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

@dataclass
class IntentRecord:
    intent_id: str
    strategy_id: str
    signal_id: str
    symbol: str
    side: str
    quantity: int

@dataclass
class OrderRecord:
    order_id: str
    intent_id: str
    state: OrderState
    broker_order_id: Optional[str] = None
    seqno: Optional[str] = None
    ordno: Optional[str] = None
    filled_quantity: int = 0
    remaining_quantity: int = 0

@dataclass
class DealRecord:
    deal_id: str
    order_id: str
    broker_trade_id: Optional[str] = None
    exchange_seq: Optional[str] = None
    price: float = 0.0
    quantity: int = 0
```

Source pattern: [VERIFIED: docs/example_order_manager.py:49-97]

### Deal-applier boundary

```python
def apply_confirmed_deal(position_anchor, order_record, deal):
    order_record.filled_quantity += deal.quantity
    order_record.remaining_quantity = max(order_record.remaining_quantity - deal.quantity, 0)

    # only here mutate position / cost basis
    if deal.side == "BUY":
        position_anchor.execute_signal("BUY", deal.price, deal.timestamp, lots=deal.quantity)
    else:
        position_anchor.execute_signal("SELL", deal.price, deal.timestamp, lots=deal.quantity)
```

Source pattern: [VERIFIED: strategies/futures/monitor.py:1279-1307][VERIFIED: strategies/futures/squeeze_futures/engine/simulator.py:216-320]

## State of the Art

| Old Approach | Current Approach | Impact |
|---|---|---|
| Flat trade row as truth | Intent/order/deal separation with reconciliation | Needed for partial fill, cancel/reject, restart safety. [CITED: docs/shioaji_訂單生命週期.md] |
| `place_order()` return treated as final state | Callback + `update_status()` dual-track | Required to avoid missing IDs/status and callback gaps. [CITED: docs/shioaji_訂單生命週期.md] |
| Position changes on intent/submit | Position changes on confirmed deal application | Prevents false holdings/cost basis drift. [VERIFIED: RULES.md:10-25] |

**Deprecated/outdated in this repo context:**
- Submitting live futures order and then immediately mutating `PaperTrader` in the same flow. [VERIFIED: strategies/futures/monitor.py:1404-1519]
- Ignoring order-status callbacks in live options lifecycle. [VERIFIED: strategies/options/live_options_squeeze_monitor.py:1212-1219]

## Assumptions Log

| # | Claim | Risk if Wrong |
|---|---|---|
| A1 | Local `deal_id` should be introduced even if broker `trade_id` exists. [ASSUMED] | Medium — planner may over-design identifiers if broker IDs are always sufficient. |
| A2 | Options should eventually use the same deal-applier contract shape even if the current anchor remains monitor-local state. [ASSUMED] | Medium — affects how much Phase 1 changes options internals. |

## Open Questions (RESOLVED)

1. **Does Phase 1 unify only the contract, or also the options position anchor API?**
    - What we know: `RULES.md` says futures truth is `PaperTrader.position` while options truth is `ShioajiOptionsSmartMonitor.position`. [VERIFIED: RULES.md:20-25]
    - Resolution: Phase 1 unifies the shared lifecycle contract and deal-applier interface only; it does **not** replace the options position anchor API in this phase.
    - Reason: forcing an options state-store rewrite would expand scope into Phase 2/3 concerns and increase runtime risk before the contract is stable.

2. **Should `custom_field` be the broker-side correlation key for live orders?**
    - What we know: the Shioaji lifecycle notes recommend preserving `custom_field` for traceability. [CITED: docs/shioaji_訂單生命週期.md]
    - What’s unclear: current live builders do not set it. [VERIFIED: strategies/options/options_engine/engine/broker_adapter.py:27-49][VERIFIED: strategies/futures/squeeze_futures/data/shioaji_client.py:212-231]
    - Resolution: `custom_field` is optional in Phase 1; the contract will support it, but broker-side propagation is deferred unless it can be added without destabilizing live execution.
    - Reason: Phase 1 must first make local `intent_id` / `order_id` / `deal_id` and existing broker IDs sufficient for traceability before adding another live-order dependency.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|---|---|---|---|---|
| Python | contract/tests | ✓ | 3.12.5 | — |
| pytest | validation | ✓ | 9.0.2 | — |
| Shioaji | live-path modeling reference | ✓ | 1.3.3 | Use paper-mode tests if live API unavailable |
| Node | existing tooling | ✓ | v20.20.0 | — |

[VERIFIED: local environment]

## Validation Architecture

### Test Framework

| Property | Value |
|---|---|
| Framework | pytest 9.0.2 [VERIFIED: local environment] |
| Config file | `pytest.ini` [VERIFIED: pytest.ini:1-4] |
| Quick run command | `python3 -m pytest tests/test_order_lifecycle -v` |
| Full suite command | `python3 -m pytest tests/ -v` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|---|---|---|---|---|
| EXEC-01 | intent/order/deal linkage persists through paper/live/recovery | unit/integration | `python3 -m pytest tests/test_order_lifecycle/test_contract_traceability.py -v` | ❌ Wave 0 |
| EXEC-02 | accepted/partial/full/cancel/reject remain distinct | unit | `python3 -m pytest tests/test_order_lifecycle/test_order_manager.py -v` | ✅ partial/cancel/reject only |
| EXEC-03 | position/cost basis mutate only on confirmed deal application | integration/system | `python3 -m pytest tests/test_order_lifecycle/test_position_apply_on_confirmed_deal.py -v` | ❌ Wave 0 |

### Wave 0 Gaps

- `tests/test_order_lifecycle/test_contract_traceability.py` — new IDs and linkage
- `tests/test_order_lifecycle/test_position_apply_on_confirmed_deal.py` — block optimistic mutation on submit
- Expand live-path tests to cover order-status callbacks separately from deal callbacks
- Add regression around futures live path currently updating `PaperTrader` after submit

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---|---|---|
| V2 Authentication | no | Existing runtime auth |
| V3 Session Management | no | Existing Shioaji session handling |
| V4 Access Control | no | N/A for this phase |
| V5 Input Validation | yes | Typed contract enums/dataclasses and guarded status mapping |
| V6 Cryptography | no | N/A |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---|---|---|
| Duplicate execution from ambiguous pending state | Tampering | Explicit pending/accepted/deal separation + duplicate-submit guard |
| False position from optimistic submit mutation | Integrity | Deal-only position applier |
| Lost cancel/reject truth after callback gaps | Repudiation/Integrity | Preserve order events + later reconciliation |
| Dashboard/operator mismatch vs engine truth | Integrity | Export dashboard files from lifecycle truth, not ledger inference |

## Sources

### Primary
- `.planning/REQUIREMENTS.md` — phase requirements and traceability [VERIFIED]
- `RULES.md` — project safety constraints [VERIFIED]
- `docs/example_order_manager.py` — target contract shape for intent/order/fill separation [VERIFIED]
- `core/order_management/*` — current shared model and gaps [VERIFIED]
- `strategies/futures/monitor.py` — current futures mutation and export paths [VERIFIED]
- `strategies/options/live_options_squeeze_monitor.py` — current options callback, position, export, recovery paths [VERIFIED]
- `ui/dashboard.py` and `core/dashboard_positions.py` — current 8500/dashboard compatibility targets [VERIFIED]

### Secondary
- `docs/shioaji_訂單生命週期.md` — repo-local research note citing official Shioaji behavior around `update_status()`, order/deal separation, and identifier fields [CITED]

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — mostly codebase-verified reuse.  
- Architecture: MEDIUM — codebase + example are strong, but broker-field details rely on repo-local Shioaji notes.  
- Pitfalls: HIGH — directly visible in current code paths.

**Valid until:** 30 days

---

## RESEARCH COMPLETE

**Phase:** 1 - Lifecycle Truth Contract  
**Confidence:** MEDIUM

### Key Findings
- Futures live currently risks violating EXEC-03 because submit flow can still mutate `PaperTrader` before confirmed fill. [VERIFIED]
- Options live only processes deal callbacks, so accepted/cancel/reject state is currently not preserved. [VERIFIED]
- Existing `core.order_management`, dashboard `*_orders.json`, and `tests/test_order_lifecycle/` should be reused, not replaced. [VERIFIED]
- Current shared model lacks `intent_id` and durable deal identifiers, so EXEC-01 is not satisfiable without contract expansion. [VERIFIED]
- Recovery/dashboard helpers use synthetic IDs and flattened logs, so they must remain read models, not truth sources. [VERIFIED]

### Ready for Planning
Planner should create tasks around:
1. shared `Intent -> Order -> Deal` contract and IDs,
2. order-state vs deal-state separation,
3. deal-only position/cost-basis application with dashboard/export compatibility.
