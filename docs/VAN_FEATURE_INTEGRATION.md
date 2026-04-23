# tw-trading-unified Feature Integration Spec

## 0. Purpose

Define how `tw-trading-unified` consumes external stock features produced by `tw-canslim-web`.

This integration layer transforms external features into:

- universe filters
- signal enhancements
- position sizing modifiers
- adaptive tuning inputs

The execution layer remains the source of truth for orders, fills, positions, and reconciliation.

## 1. Responsibility Boundary

### `tw-canslim-web` owns

- feature generation
- ranking generation
- monthly and fundamental feature computation
- feature publication

### `tw-trading-unified` owns

- loading feature snapshots
- validating schema freshness
- combining features with intraday signals
- making trade decisions
- execution and risk control

### `tw-trading-unified` must not do

- recompute monthly revenue features
- duplicate feature logic already owned by `tw-canslim-web`
- treat stale external features as hard truth
- mix feature computation with execution state management

## 2. Input Data

### Source files

```text
tw-canslim-web/api/stock_features.json
tw-canslim-web/api/ranking.json
```

### Loader contract

```python
features = load_json("stock_features.json")
ranking = load_json("ranking.json")
```

The trading layer should load these as read-only feature snapshots.

## 3. Expected Feature Fields

The exact schema may evolve, but the trading layer currently expects fields in this family:

| Field | Meaning | Usage |
| --- | --- | --- |
| `symbol` | Stock code | Join key |
| `rev_yoy` | Revenue YoY growth | Universe filter |
| `rev_accelerating` | Revenue acceleration flag | Signal enhancement / risk adjustment |
| `revenue_score` | Revenue quality score | Position sizing / minimum quality gate |
| `breakout_score` | Breakout quality score | Signal ranking |
| `volume_score` | Volume confirmation score | Signal ranking |

If new fields are added, they must be backward compatible or guarded with defaults.

## 4. Feature Usage Rules

### 4.1 Universe Filter

```python
if stock.rev_yoy < 0.2:
    skip()
```

External features may be used to exclude weak candidates before intraday logic runs.

### 4.2 Signal Enhancement

```python
if breakout and stock.rev_accelerating:
    signal_strength += 1
```

Feature data should strengthen or weaken an existing intraday setup, not replace it entirely.

### 4.3 Position Sizing

```python
if stock.revenue_score >= 5:
    position_size *= 1.2
```

Position sizing adjustments must still respect the live risk budget and capital controls in `tw-trading-unified`.

### 4.4 Risk Adjustment

```python
if not stock.rev_accelerating:
    tighten_stop_loss()
```

Feature weakness may reduce conviction or tighten risk, but must not bypass execution safeguards.

## 5. Strategy Integration Example

```python
def generate_signal(stock):
    if stock.revenue_score < 4:
        return None

    if not early_breakout(stock):
        return None

    if not volume_spike(stock):
        return None

    return "BUY"
```

Interpretation:

1. external features qualify the candidate
2. intraday price/volume confirms the setup
3. execution layer handles the actual order lifecycle

## 6. Adaptive System Integration

### Example feature vector

```python
X = [
    revenue_score,
    rev_acceleration,
    breakout_score,
    volume_score,
]
```

### Optimization targets

- maximize win rate
- maximize risk-adjusted return
- minimize drawdown

These features may feed tuning systems, but tuning output must still pass through the decision and risk layers.

## 7. Execution Layer Contract

The following execution surfaces remain unchanged:

- order lifecycle
- reconciliation
- position tracking
- fill handling
- PnL accounting

Feature integration must not interfere with execution truth.

**Rule:** feature data is advisory input; execution state is operational truth.

## 8. Data Refresh Rules

Feature snapshots may be refreshed:

- every `N` minutes
- on a new batch release
- at process startup

Recommended behavior:

1. load latest successful feature snapshot
2. validate timestamp / freshness
3. swap atomically into decision use
4. never block trading execution on refresh failure

## 9. Failover Rules

### Missing feature

```python
use_default_signal()
```

### Stale feature

```python
reduce_position_size()
```

### Parse or schema failure

- log warning
- fall back to safe defaults
- continue trading with degraded feature support

The system must degrade safely rather than crash.

## 10. Logging Requirements

Each feature-driven decision should log enough context to support post-trade analysis.

Minimum useful fields:

- `feature_snapshot_used`
- `signal_reason`
- `feature_weight`
- `feature_timestamp`
- `schema_version`

## 11. Anti-Patterns

Do **not**:

- recompute `rev_yoy` in the trading layer
- mix feature computation with execution logic
- trust stale feature data blindly
- let external feature fetch failure break the dashboard or trading loop
- overwrite execution state using feature state

## 12. Final Principle

> Features are static truth. Execution is dynamic truth. Never mix the two layers.

## 13. One-Line Summary

**`tw-canslim-web` = Feature Factory**  
**`tw-trading-unified` = Decision Engine**
