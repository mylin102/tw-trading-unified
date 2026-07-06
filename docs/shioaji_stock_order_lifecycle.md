下面給你兩段可直接放進 repo 的內容：

1. `stock_order_lifecycle_spec.md`
2. `python example`：股票版 order manager extension

我先把重點講清楚：

**股票版和期貨版最大的工程差異，不是下單 API，而是「成交後還有交割層」**。
Shioaji 對股票同樣要求你在 `place_order()` 後用 `update_status()` 更新 `Trade` 狀態；同時它也提供 `account_balance`、`list_positions`、`settlements` 等帳務查詢能力。`settlements` 會回傳 `T=0/T=1/T=2` 的交割資訊，這正是你在股票 execution model 裡要補的 settlement layer。([Sinotrade][1])

---

````md
# stock_order_lifecycle_spec.md

## 0. Purpose

Define a stock-specific order lifecycle model for Shioaji-based trading system.

This spec extends the generic order lifecycle by adding:
- stock settlement tracking
- available cash constraints
- available inventory constraints
- T+0 / T+1 / T+2 accounting awareness

This spec is intended for Taiwan stock trading through Shioaji.

---

## 1. Why stock is different from futures

For futures:
- execution and position update are the primary concerns

For stocks:
- execution is not the whole story
- settlement must also be tracked
- available cash and available inventory must be validated

### Engineering interpretation

A stock order has at least 4 layers:

1. Intent
2. Broker Order
3. Fill / Execution
4. Settlement

Execution is not equal to accounting finalization.

---

## 2. External truth sources

### 2.1 Order status

Shioaji requires `update_status()` to refresh `Trade` state after `place_order()`.
Therefore, local order state must not rely only on initial API return.

### 2.2 Position truth

Use `list_positions` as broker-side account position reference.

### 2.3 Cash truth

Use `account_balance` as account cash reference.

### 2.4 Settlement truth

Use `settlements` as broker-side settlement reference.
Settlement records include:
- `date`
- `amount`
- `T`

This gives a broker-side view of T+0 / T+1 / T+2 settlement flow.

---

## 3. Stock-specific state model

### 3.1 Intent / Order / Fill / Settlement

| Layer | Meaning |
|---|---|
| Intent | Strategy wants to buy or sell stock |
| Order | Order submitted to broker |
| Fill | Actual trade execution |
| Settlement | Cash / stock settlement accounting completion |

### 3.2 Order execution states

```text
INTENT_CREATED
-> SUBMITTING
-> PENDING_SUBMIT
-> SUBMITTED
-> PARTIALLY_FILLED
-> FILLED
-> CANCEL_REQUESTED
-> CANCELLED
-> FAILED
-> UNKNOWN
````

### 3.3 Settlement states

```text
NOT_APPLICABLE
PENDING_SETTLEMENT
SETTLING_T0
SETTLING_T1
SETTLING_T2
SETTLED
SETTLEMENT_UNKNOWN
```

### 3.4 State meaning

| Settlement State   | Meaning                                      |
| ------------------ | -------------------------------------------- |
| NOT_APPLICABLE     | For orders not yet filled                    |
| PENDING_SETTLEMENT | Filled, waiting for settlement tracking      |
| SETTLING_T0        | Settlement record exists at T=0              |
| SETTLING_T1        | Settlement record exists at T=1              |
| SETTLING_T2        | Settlement record exists at T=2              |
| SETTLED            | Settlement completed / no longer outstanding |
| SETTLEMENT_UNKNOWN | Broker-side accounting mismatch              |

---

## 4. Stock-specific constraints

### 4.1 Buy-side constraint

Before a buy order:

* validate strategy cash budget
* validate account-level available cash guardrail
* record expected settlement obligation

### 4.2 Sell-side constraint

Before a sell order:

* validate available inventory
* do not rely only on strategy-local position
* reconcile against broker position snapshot

### 4.3 Fill is not accounting finality

After `FILLED`:

* order execution is done
* settlement tracking begins
* local accounting state must move into settlement lifecycle

---

## 5. Data model

### 5.1 StockOrderRecord

```python
@dataclass
class StockOrderRecord:
    order_id: str
    intent_id: str
    symbol: str
    side: str
    quantity: int
    price: Optional[float]

    state: str
    settlement_state: str

    filled_quantity: int
    remaining_quantity: int

    broker_order_id: Optional[str]
    seqno: Optional[str]
    ordno: Optional[str]

    created_at: datetime
    updated_at: datetime

    fill_completed_at: Optional[datetime]
    settlement_due_date: Optional[date]

    expected_cash_delta: float
    expected_inventory_delta: int

    raw_trade: Any
    metadata: dict
```

### 5.2 SettlementLedgerEntry

```python
@dataclass
class SettlementLedgerEntry:
    settlement_date: date
    amount: float
    t_day: int
    source: str = \"broker_settlements\"
```

---

## 6. Required invariants

### 6.1 Execution invariants

* filled_quantity <= quantity
* remaining_quantity = quantity - filled_quantity
* FILLED implies remaining_quantity == 0

### 6.2 Inventory invariants

For sell orders:

* intended sell qty must not exceed available inventory guardrail

### 6.3 Cash invariants

For buy orders:

* expected cash obligation must be recorded at fill time
* cash must not be double-counted as reusable before internal policy allows it

### 6.4 Settlement invariants

If order state == FILLED:

* settlement_state must not remain NOT_APPLICABLE

---

## 7. Reconciliation model

### 7.1 Order reconciliation

Use:

* `update_status(account=api.stock_account, trade=trade)`

Purpose:

* refresh stock order state
* verify whether order is still modifiable / cancellable
* resolve missing-order visibility

### 7.2 Position reconciliation

Use:

* `list_positions(api.stock_account)`

Purpose:

* verify broker-side inventory
* detect local vs broker position mismatch

### 7.3 Cash reconciliation

Use:

* `account_balance()`

Purpose:

* verify broker-side cash snapshot
* support cash guardrails

### 7.4 Settlement reconciliation

Use:

* `settlements(api.stock_account)`

Purpose:

* detect outstanding settlement obligations
* track T+0 / T+1 / T+2 lifecycle
* verify internal settlement ledger consistency

---

## 8. Recommended processing flow

### 8.1 Before placing stock order

#### Buy

1. refresh cash snapshot if stale
2. verify available cash guardrail
3. create intent
4. submit order
5. track order lifecycle

#### Sell

1. refresh position snapshot if stale
2. verify available inventory
3. create intent
4. submit order
5. track order lifecycle

### 8.2 After fill

1. mark execution state as FILLED
2. create settlement obligation
3. set settlement_state = PENDING_SETTLEMENT
4. schedule settlement reconciliation

### 8.3 Settlement reconciliation loop

1. query broker settlements
2. match by settlement date / amount / expected direction
3. update settlement state:

   * T0 -> SETTLING_T0
   * T1 -> SETTLING_T1
   * T2 -> SETTLING_T2
4. once obligation is no longer outstanding, mark SETTLED

---

## 9. Alerts

### P0

* SELL_WITHOUT_AVAILABLE_INVENTORY
* BUY_WITHOUT_CASH_GUARDRAIL
* FILLED_BUT_NO_SETTLEMENT_TRACKING
* ORDER_VISIBLE_MISMATCH

### P1

* SETTLEMENT_LEDGER_MISMATCH
* POSITION_RECON_MISMATCH
* CASH_RECON_MISMATCH

### P2

* SETTLEMENT_STUCK_T0
* SETTLEMENT_STUCK_T1
* SETTLEMENT_STUCK_T2

---

## 10. Anti-patterns

Do NOT:

* treat `place_order()` return as final truth
* assume FILLED means accounting completion
* use only local position for sell eligibility
* use only raw cash value without settlement-aware guardrail
* skip settlement reconciliation
* assume stock lifecycle == futures lifecycle

---

## 11. Final principle

> For stocks, execution truth and settlement truth are different layers.

A robust stock trading engine must track both.

````

---

下面是 Python 範例。這版不是完整產品，而是你可以直接接進現有 `order_manager.py` 的**股票擴充骨架**。

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from typing import Any, Dict, List, Optional


class OrderState(str, Enum):
    INTENT_CREATED = "INTENT_CREATED"
    SUBMITTING = "SUBMITTING"
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class SettlementState(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING_SETTLEMENT = "PENDING_SETTLEMENT"
    SETTLING_T0 = "SETTLING_T0"
    SETTLING_T1 = "SETTLING_T1"
    SETTLING_T2 = "SETTLING_T2"
    SETTLED = "SETTLED"
    SETTLEMENT_UNKNOWN = "SETTLEMENT_UNKNOWN"


@dataclass
class StockIntent:
    intent_id: str
    strategy_id: str
    signal_id: str
    symbol: str
    side: str  # BUY / SELL
    quantity: int
    price: Optional[float]
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class StockOrderRecord:
    order_id: str
    intent_id: str
    symbol: str
    side: str
    quantity: int
    price: Optional[float]

    state: OrderState = OrderState.INTENT_CREATED
    settlement_state: SettlementState = SettlementState.NOT_APPLICABLE

    filled_quantity: int = 0
    remaining_quantity: int = 0

    broker_order_id: Optional[str] = None
    seqno: Optional[str] = None
    ordno: Optional[str] = None

    fill_completed_at: Optional[datetime] = None
    settlement_due_date: Optional[date] = None

    expected_cash_delta: float = 0.0
    expected_inventory_delta: int = 0

    raw_trade: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SettlementLedgerEntry:
    settlement_date: date
    amount: float
    t_day: int
    raw_payload: Dict[str, Any] = field(default_factory=dict)


class StockLifecycleError(Exception):
    pass


class StockOrderManager:
    def __init__(self, api: Any):
        self.api = api
        self.orders: Dict[str, StockOrderRecord] = {}
        self.position_cache: Dict[str, int] = {}
        self.settlement_ledger: List[SettlementLedgerEntry] = []

    # -----------------------------
    # Broker snapshot helpers
    # -----------------------------
    def refresh_stock_trade(self, trade: Any) -> Any:
        """
        Official stock flow requires update_status() to refresh trade state.
        """
        self.api.update_status(account=self.api.stock_account, trade=trade)
        return trade

    def get_cash_snapshot(self) -> Any:
        """
        Uses stock account balance snapshot.
        """
        return self.api.account_balance()

    def get_position_snapshot(self) -> List[Any]:
        """
        Uses stock account positions snapshot.
        """
        return self.api.list_positions(self.api.stock_account)

    def get_settlement_snapshot(self) -> List[Any]:
        """
        Uses stock settlements snapshot.
        """
        return self.api.settlements(self.api.stock_account)

    # -----------------------------
    # Guardrails
    # -----------------------------
    def ensure_sellable_inventory(self, symbol: str, sell_qty: int) -> None:
        broker_positions = self.get_position_snapshot()
        broker_qty = 0

        for pos in broker_positions:
            code = getattr(pos, "code", None) or getattr(pos, "symbol", None)
            qty = getattr(pos, "quantity", 0) or 0
            if code == symbol:
                broker_qty = int(qty)
                break

        self.position_cache[symbol] = broker_qty

        if broker_qty < sell_qty:
            raise StockLifecycleError(
                f"SELL_WITHOUT_AVAILABLE_INVENTORY symbol={symbol} sell_qty={sell_qty} broker_qty={broker_qty}"
            )

    def ensure_cash_guardrail(self, est_notional: float, cash_buffer: float = 0.0) -> None:
        """
        Conservative guardrail.
        Do not assume all balance is safely reusable.
        """
        balance = self.get_cash_snapshot()
        # Adapt this to your actual account_balance structure
        available_cash = float(getattr(balance, "acc_balance", 0.0) or 0.0)

        if available_cash < est_notional + cash_buffer:
            raise StockLifecycleError(
                f"BUY_WITHOUT_CASH_GUARDRAIL need={est_notional + cash_buffer:.2f} available={available_cash:.2f}"
            )

    # -----------------------------
    # Order submit
    # -----------------------------
    def submit_stock_order(
        self,
        intent: StockIntent,
        contract: Any,
        order: Any,
    ) -> StockOrderRecord:
        if intent.side == "SELL":
            self.ensure_sellable_inventory(intent.symbol, intent.quantity)
        elif intent.side == "BUY":
            est_notional = float(intent.price or 0.0) * intent.quantity
            self.ensure_cash_guardrail(est_notional=est_notional)

        record = StockOrderRecord(
            order_id=f"stock_order_{intent.intent_id}",
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=intent.price,
            state=OrderState.SUBMITTING,
            remaining_quantity=intent.quantity,
        )

        trade = self.api.place_order(contract, order)
        record.raw_trade = trade

        self.refresh_stock_trade(trade)

        status = str(getattr(trade.status, "status", "") or "")
        record.state = self._map_trade_status(status)

        record.broker_order_id = self._safe_str(getattr(trade.order, "id", None))
        record.seqno = self._safe_str(getattr(trade.status, "seqno", None))
        record.ordno = self._safe_str(getattr(trade.status, "ordno", None))

        self.orders[record.order_id] = record
        return record

    # -----------------------------
    # Fill + settlement transition
    # -----------------------------
    def apply_fill(self, order_id: str, fill_qty: int, fill_price: float, fill_time: datetime) -> None:
        rec = self.orders[order_id]

        rec.filled_quantity += fill_qty
        rec.remaining_quantity = max(rec.quantity - rec.filled_quantity, 0)

        if rec.remaining_quantity == 0:
            rec.state = OrderState.FILLED
            rec.fill_completed_at = fill_time
            rec.settlement_state = SettlementState.PENDING_SETTLEMENT
            rec.settlement_due_date = self._estimate_t_plus_2(fill_time.date())

            signed_qty = rec.quantity if rec.side == "BUY" else -rec.quantity
            signed_cash = -(fill_price * fill_qty) if rec.side == "BUY" else +(fill_price * fill_qty)

            rec.expected_inventory_delta = signed_qty
            rec.expected_cash_delta += signed_cash
        else:
            rec.state = OrderState.PARTIALLY_FILLED

    # -----------------------------
    # Settlement reconciliation
    # -----------------------------
    def reconcile_settlements(self) -> None:
        raw_settlements = self.get_settlement_snapshot()
        self.settlement_ledger = []

        for s in raw_settlements:
            entry = SettlementLedgerEntry(
                settlement_date=self._coerce_date(getattr(s, "date", None)),
                amount=float(getattr(s, "amount", 0.0) or 0.0),
                t_day=int(getattr(s, "T", -1)),
                raw_payload={
                    "date": getattr(s, "date", None),
                    "amount": getattr(s, "amount", None),
                    "T": getattr(s, "T", None),
                },
            )
            self.settlement_ledger.append(entry)

        for rec in self.orders.values():
            if rec.state != OrderState.FILLED:
                continue

            if rec.settlement_state == SettlementState.NOT_APPLICABLE:
                rec.settlement_state = SettlementState.PENDING_SETTLEMENT

            matches = [
                x for x in self.settlement_ledger
                if rec.settlement_due_date is not None and x.settlement_date == rec.settlement_due_date
            ]

            if not matches:
                continue

            t_values = {m.t_day for m in matches}

            if 0 in t_values:
                rec.settlement_state = SettlementState.SETTLING_T0
            elif 1 in t_values:
                rec.settlement_state = SettlementState.SETTLING_T1
            elif 2 in t_values:
                rec.settlement_state = SettlementState.SETTLING_T2
            else:
                rec.settlement_state = SettlementState.SETTLEMENT_UNKNOWN

    # -----------------------------
    # Helpers
    # -----------------------------
    @staticmethod
    def _map_trade_status(status: str) -> OrderState:
        mapping = {
            "PendingSubmit": OrderState.PENDING_SUBMIT,
            "PreSubmitted": OrderState.PENDING_SUBMIT,
            "Inactive": OrderState.PENDING_SUBMIT,
            "Submitted": OrderState.SUBMITTED,
            "PartFilled": OrderState.PARTIALLY_FILLED,
            "Filled": OrderState.FILLED,
            "Cancelled": OrderState.CANCELLED,
            "Failed": OrderState.FAILED,
        }
        return mapping.get(status, OrderState.UNKNOWN)

    @staticmethod
    def _safe_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _coerce_date(value: Any) -> date:
        if isinstance(value, date):
            return value
        return datetime.fromisoformat(str(value)).date()

    @staticmethod
    def _estimate_t_plus_2(trade_date: date) -> date:
        """
        Simplified example only.
        In production, replace with TWSE/TPEX trading calendar logic.
        """
        from datetime import timedelta

        d = trade_date
        added = 0
        while added < 2:
            d = d + timedelta(days=1)
            if d.weekday() < 5:
                added += 1
        return d
````

這段 Python 主要表達三件事：

* **股票成交後，要從 `FILLED` 進到 settlement tracking**
* **買單要加 cash guardrail，賣單要加 inventory guardrail**
* **settlement reconciliation 是獨立 loop，不要混在純 order callback 裡**

Shioaji 官方文件能直接支持這個設計方向，因為它把股票的 `update_status()`、`account_balance()`、`list_positions()`、`settlements()` 都分開提供；同時它也有 order/deal event 流，表示 execution 與帳務本來就不是同一層。([Sinotrade][1])

另外，你做 reconciliation loop 時要注意查詢限制：官方文件列出帳務查詢如 `list_positions`、`account_balance`、`settlements` 等，總量是 **5 秒內 25 次**；委託相關如 `place_order`、`update_status`、`update_order`、`cancel_order` 是 **10 秒內 500 次**。所以 stock settlement loop 不要設計得太密。([Sinotrade][2])


[1]: https://sinotrade.github.io/tutor/order/Stock/?utm_source=chatgpt.com "Stock - Shioaji - Taiwan's Leading Cross Platform Trading ..."
[2]: https://sinotrade.github.io/tutor/limit/?utm_source=chatgpt.com "Taiwan's Leading Cross Platform Trading API"

