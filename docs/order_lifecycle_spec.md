# order_lifecycle_spec.md

# Order Lifecycle Spec for Futures Auto-Trading

This document defines the engineering contract for order lifecycle handling in a futures auto-trading system.  
Target use case: Taiwan futures / Shioaji-style live trading with strategy routing, signal gating, and execution auditing.

---

## 1. Purpose

The goal is to prevent these common production failures:

- duplicate orders from multiple monitors
- opposite-side orders on the same bar
- missing order state transitions
- stale working orders remaining alive after signal invalidation
- strategy engine believing an order exists while broker says it does not
- position state drifting away from broker truth

This spec separates:

1. **Signal state**
2. **Order state**
3. **Position state**
4. **Broker truth**
5. **Risk state**

These must never be mixed casually inside one `if/else` block.

---

## 2. Core Principles

## 2.1 Single source of truth
Broker/exchange callbacks and broker query reconciliation are the source of truth for final order state.

## 2.2 Strategy is not execution
A signal such as `BUY` is only intent.  
It is **not** equal to:
- submitted
- accepted
- filled
- positioned

## 2.3 One decision per bar per symbol
For the same symbol and same strategy evaluation timestamp:

- only one net direction may be active
- long and short signals must be mutually exclusive
- re-entry rules must be explicit

## 2.4 Explicit state machine
Orders must move through named states only. Never infer state from partial flags.

---

## 3. State Models

## 3.1 Signal State

Possible states:

- `NONE`
- `LONG_INTENT`
- `SHORT_INTENT`
- `EXIT_INTENT`
- `CANCEL_INTENT`

This belongs to the strategy layer.

## 3.2 Order State

Possible states:

- `CREATED`
- `SUBMITTING`
- `SUBMITTED`
- `ACKNOWLEDGED`
- `PARTIALLY_FILLED`
- `FILLED`
- `CANCEL_PENDING`
- `CANCELED`
- `REJECTED`
- `EXPIRED`
- `ERROR`

This belongs to the execution layer.

## 3.3 Position State

Possible states:

- `FLAT`
- `LONG`
- `SHORT`
- `FLATTENING`

This belongs to portfolio / broker reconciliation.

---

## 4. Order Lifecycle

## 4.1 Entry flow

```text
Signal generated
→ risk checks pass
→ order ticket created
→ submit to broker
→ broker acknowledges
→ partial fill(s) possible
→ full fill
→ position state updated
```

## 4.2 Cancel flow

```text
working order exists
→ signal invalidates or timeout hit
→ cancel requested
→ broker confirms cancel
→ order becomes canceled
→ strategy may unlock re-entry
```

## 4.3 Reject flow

```text
submit order
→ broker rejects
→ state = REJECTED
→ unlock strategy
→ log rejection reason
→ optionally cooldown before retry
```

---

## 5. Required Data Structures

## 5.1 Signal Record

Each evaluated signal should persist at least:

- `signal_id`
- `strategy_name`
- `symbol`
- `side`
- `bar_timestamp`
- `trading_day`
- `signal_price`
- `stop_loss`
- `take_profit`
- `signal_confidence`
- `regime`
- `feature_snapshot`
- `created_at`

## 5.2 Order Record

Each order should persist at least:

- `client_order_id`
- `broker_order_id`
- `signal_id`
- `symbol`
- `side`
- `order_type`
- `price`
- `quantity`
- `status`
- `filled_qty`
- `avg_fill_price`
- `submit_time`
- `last_update_time`
- `reject_reason`
- `cancel_reason`

## 5.3 Position Record

Each symbol should persist:

- `symbol`
- `net_qty`
- `direction`
- `avg_cost`
- `unrealized_pnl`
- `realized_pnl`
- `last_broker_sync_time`

### Position PnL Rule

> **庫存用 `Unit.Share.quantity`，損益用 `p.pnl`；自算 PnL 只做內部估算，不拿來覆蓋券商回傳值。**

`list_positions(unit=Unit.Share)` 作為股票完整庫存主來源：

| 欄位 | 意義 |
|------|------|
| `quantity` | 股數，`Unit.Share` 下為完整股數 |
| `yd_quantity` | 昨倉股數 |
| `price` | 平均成本價 |
| `last_price` | 現價 |
| `pnl` | 券商後端回傳的未實現損益 |

重點結論：

> `pnl` 不是 Python 端用 `(last_price - price) × quantity` 即時計算出來的，而是 Shioaji / 券商後端直接回傳的未實現損益欄位。

官方文件也把 `list_positions` 定義為查詢帳戶**未實現損益**，`pnl` 欄位為 unrealized profit / 損益；`StockPositionDetail` 另有 `fee`、`ex_dividends`、`interest` 等欄位，表示後端 PnL 可能已納入券商帳務邏輯，而不是單純價差計算。([sinotrade.github.io][1])

[1]: https://sinotrade.github.io/tutor/accounting/position/

建議工程規則：

```md
## Position PnL Rule

- Use `Unit.Share` as the source of truth for stock quantity.
- Use `p.pnl` as the broker-reported unrealized PnL.
- Do not recalculate broker PnL using `(last_price - price) * quantity`.
- For internal risk control:
  - market_value = last_price * quantity
  - cost_basis_estimate = price * quantity
  - broker_pnl = p.pnl
- If detailed reconciliation is needed, query `list_position_detail()` and inspect fee / ex_dividends / interest.
```

一句話版本：

> **庫存用 `Unit.Share.quantity`，損益用 `p.pnl`；自算 PnL 只做內部估算，不拿來覆蓋券商回傳值。**

See `docs/position_spec.md` for full Shioaji position query spec (Unit.Common vs Unit.Share, cross-validation, caveats).

---

## 6. Risk Gates Before Submission

Before any new entry order:

- confirm trading session is open
- confirm symbol is tradable
- confirm strategy is enabled
- confirm no duplicate active signal for same bar
- confirm no opposite-side working order exists
- confirm no existing position conflicts with intended action
- confirm max position limit not exceeded
- confirm daily loss limit not breached
- confirm cooldown rules not active
- confirm data freshness is acceptable

If any check fails, do not submit.

---

## 7. Mutual Exclusion Rules

These are critical.

## 7.1 No simultaneous long and short entry
For the same symbol and bar:
- `BUY` and `SELL` entry cannot coexist

## 7.2 One active strategy per symbol
Recommended policy:
- one router output per symbol
- one execution intent per bar
- one net position direction

## 7.3 Lock after submission
After an entry order is submitted:
- block new entry signals until fill/cancel/reject resolution
- unless explicit scale-in logic exists

---

## 8. Working Order Management

A working order is any order not final:

- `SUBMITTING`
- `SUBMITTED`
- `ACKNOWLEDGED`
- `PARTIALLY_FILLED`
- `CANCEL_PENDING`

For every working order, monitor:

- age in seconds
- distance from market
- whether underlying signal still valid
- whether session end is approaching
- whether opposite signal appeared
- whether broker still reports it active

### Suggested timeouts

Example policy for intraday futures:
- market order: must transition quickly or mark as suspicious
- limit entry: cancel if not filled within N bars
- session nearing end: do not leave passive entry orders alive

---

## 9. Broker Reconciliation

This is mandatory in production.

At fixed intervals:
1. query broker open orders
2. query broker positions
3. query recent fills
4. compare with local state
5. repair divergence

### Examples of divergence handling

- local says `SUBMITTED`, broker has no such order  
  → mark as unknown, investigate, reconcile

- local says `FLAT`, broker says long 1 lot  
  → local position must be corrected immediately

- broker reports fill callback was missed  
  → reconstruct from trade history

---

## 10. Event Handling Model

Use event-driven updates whenever possible.

Recommended event types:

- `signal_generated`
- `order_created`
- `order_submitted`
- `order_acknowledged`
- `order_partially_filled`
- `order_filled`
- `order_cancel_requested`
- `order_canceled`
- `order_rejected`
- `position_opened`
- `position_reduced`
- `position_closed`
- `broker_reconciled`
- `execution_error`

Every event should be append-only in logs.

---

## 11. Stop Loss / Take Profit Ownership

Do not let strategy code and execution code both manage exits independently without coordination.

Recommended ownership model:

- strategy defines initial stop / take-profit intent
- execution layer owns actual live exit orders
- broker reconciliation validates that exit protection exists
- when stop is modified, old stop must be canceled or replaced explicitly

---

## 12. Session Rules for Futures

For intraday futures, define:

- allowed entry windows
- no-new-entry cutoff time
- forced-exit cutoff time
- overnight holding policy
- treatment across day/night sessions
- holiday / half-day handling

A strategy may be correct but still fail operationally if session rules are not explicit.

---

## 13. Audit Logging Requirements

Every important transition must be logged with:

- timestamp
- symbol
- strategy
- signal_id
- client_order_id
- broker_order_id
- old_state
- new_state
- reason
- relevant market snapshot

Minimum log categories:

- strategy log
- execution log
- broker callback log
- reconciliation log
- risk log
- fill log

---

## 14. Failure Scenarios to Handle

The engine must survive:

1. duplicate monitor process
2. process restart during working order
3. callback loss
4. temporary broker API disconnect
5. stale market data
6. order rejected due to account/contract issue
7. position exists but local memory reset
8. cancel request succeeds late after strategy already changed
9. partial fill then reversal signal appears
10. session boundary crossed while order still working

---

## 15. Recommended Engineering Rules

## 15.1 Use idempotent submission
For the same signal, repeated submit attempts must not create duplicate live orders.

## 15.2 Persist before submit
Write local order record first, then call broker API.

## 15.3 Final states are terminal
These are terminal:
- `FILLED`
- `CANCELED`
- `REJECTED`
- `EXPIRED`

No further transitions should leave a terminal state except via explicit reconciliation repair.

## 15.4 Position comes from fills, not assumptions
Never set position to long just because a buy order was submitted.

## 15.5 Recovery on restart
On restart:
1. load persisted local orders
2. query broker open orders
3. query broker positions
4. reconcile differences
5. only then resume strategy evaluation

---

## 16. Suggested Pseudocode

```python
signal = router.route(bar)

if signal.action == "FLAT":
    return

if has_active_working_order(symbol):
    return

if conflicts_with_existing_position(signal, symbol):
    return

if not risk_engine.approve(signal):
    return

order = execution_engine.create_order_from_signal(signal)
order_store.save(order)

broker.submit(order)
order_store.mark_submitted(order.client_order_id)
```

Callback path:

```python
def on_order_update(event):
    order_store.apply(event)
    if event.status == "FILLED":
        position_store.apply_fill(event)
```

Recovery path:

```python
def on_startup():
    local_state.load()
    broker_state = broker.query_all()
    reconciler.reconcile(local_state, broker_state)
```

---

## 17. What to Build Next

Recommended implementation order:

1. `order_state_machine.py`
2. `execution_engine.py`
3. `broker_reconciler.py`
4. `position_store.py`
5. `risk_engine.py`
6. `audit_logger.py`

---

## 18. Bottom Line

If your strategy logic is good but your order lifecycle is weak, live trading will still fail.

For production readiness, the system must guarantee:

- no duplicate entry
- no opposite orders on same bar
- no orphaned working orders
- no local/broker state drift
- restart-safe reconciliation
- complete audit trail
