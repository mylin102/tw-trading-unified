# Position Spec: Shioaji Stock Position Query & Reconciliation

## 1. Purpose

Define the correct way to query real stock positions from Shioaji API for the tw-trading-unified system.

Key requirement: **the system must know its true position** — not paper/locally-tracked, but what the broker actually holds.

## 2. The Core Discovery

Shioaji has **no single API** that returns a complete stock inventory.

You must combine two calls:

| API | Unit | What It Returns |
|-----|------|-----------------|
| `list_positions(account=api.stock_account)` | 張 (board lots) | 整股 only. 00885 → `quantity=2` = 2000 shares |
| `list_positions(account=api.stock_account, unit=Unit.Share)` | 股 (shares) | **Usually** total shares (board lots × 1000 + odd lots) |

### Practical Finding

In the tested environment (Apr 2026), `Unit.Share` returned complete holdings:

```
00885: Common=2張, Share=2150股  →  total = 2000 + 150 = 2150 ✓
1802:  Common=3張, Share=3000股  →  total = 3000 + 0 = 3000 ✓
00919: Common=0張, Share=841股    →  total = 0 + 841 = 841 ✓
```

`Unit.Share` **already includes** both the board lot portion (×1000) and odd lot portion in a single quantity.

## 3. Known Caveats (Production Warning)

`Unit.Share` ≠ guaranteed complete inventory in all scenarios.

| Scenario | Risk | Mitigation |
|----------|------|------------|
| **盘中同步延迟** (intraday sync lag) | Odd-lot fills may not immediately reflect in `Unit.Share` | Cross-check with `Unit.Common` + trade history |
| **T+2 未交割** (unsettled) | Position may exist in portfolio but not yet in `Unit.Share` | `api.list_settlements()` for pending settlement |
| **融资/融券/当冲** (margin/short/daytrade) | May not fully appear in `Unit.Share` | Check `cond` field on Common positions |
| **API backend inconsistency** (version/backend drift) | `Common` / `Share` can temporarily disagree | Periodic reconciliation |

## 4. Recommended Usage

### 4.1 Quick Mode (Dashboard / Display)

```python
positions = api.list_positions(account=api.stock_account, unit=Unit.Share)
```

Use `Unit.Share` as the primary source of truth for display.

### 4.2 Strict Mode (Auto-Trading System)

Cross-validate before trading decisions:

```python
def get_positions_reconciled(api) -> dict:
    """Return {code: total_shares} with cross-validation."""
    from collections import defaultdict
    from shioaji.constant import Unit

    common = api.list_positions(account=api.stock_account)
    share = api.list_positions(account=api.stock_account, unit=Unit.Share)
    share_by_code = {p.code: p for p in share}

    result = {}
    warnings = []

    for c in common:
        code = c.code
        expected_minimum = c.quantity * 1000  # board lot minimum
        actual = share_by_code.get(code)
        actual_qty = actual.quantity if actual else 0

        if actual_qty < expected_minimum:
            warnings.append(f"[POS-SYNC] {code}: Common={c.quantity}張 >= {expected_minimum} > Share={actual_qty}股 — possible sync issue")

        result[code] = actual_qty if actual_qty > 0 else (c.quantity * 1000)

    # Odd-lot-only positions (not in Common)
    for p in share:
        if p.code not in result:
            common_match = next((c for c in common if c.code == p.code), None)
            if common_match:
                result[p.code] = p.quantity
            elif p.quantity > 0:
                # Only in Share, not in Common — pure odd lot position
                result[p.code] = p.quantity

    return result, warnings
```

### 4.3 Position PnL Interpretation

Shioaji's `pnl` field is computed server-side (C extension, not Python):

```
pnl = (last_price - average_cost) × quantity — broker fees — taxes
```

**The exact formula is backend-defined** and may include:
- Weighted average cost (not FIFO)
- Broker fee discounts (not the standard 0.1425%)
- Dividend adjustments
- Corporate action adjustments

**Trust `p.pnl` as the canonical unrealized PnL.** Do not recompute from `price` and `last_price` alone — the result will differ.

Available fields on StockPosition (Unit.Share):

| Field | Type | Meaning |
|-------|------|---------|
| `code` | str | Ticker |
| `quantity` | int | Shares (not board lots) |
| `price` | float | Average cost price |
| `last_price` | float | Current market price (live) |
| `pnl` | float | Unrealized P&L (server-computed) |
| `yd_quantity` | int | Previous day's shares |
| `cond` | StockOrderCond | Cash / Margin |
| `direction` | Action | Buy / Sell |

## 5. Futures Positions

Futures use `api.list_positions(account=api.futopt_account)` (no `unit` parameter):

```python
futs = api.list_positions(account=api.futopt_account)
for p in futs:
    print(p.code, p.quantity, p.price, p.pnl)
```

Futures positions are always in contracts (not shares).

## 6. Integration Points

| System Component | Should Use |
|-----------------|------------|
| Dashboard display | `Unit.Share` (quick mode) |
| Pre-trade position check | `get_positions_reconciled()` (strict mode) |
| PnL display | `p.pnl` from Shioaji (server computed) |
| Order lifecycle reconciliation | `list_positions` + `list_trades` + `list_settlements` |
| Audit log / snapshot | `get_positions_reconciled()` → save to `positions_snapshot_YYYYMMDD.json` |

## 7. Bottom Line

> **`Unit.Share` is treated as primary source of truth, but periodically cross-validated with `Unit.Common` to detect sync inconsistencies.**

For auto-trading: always validate position from broker before entering/exiting. Never trust the locally tracked position alone.
