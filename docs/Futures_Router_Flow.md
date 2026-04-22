# futures_router_execution_flow.md

## Overview

This document defines the execution flow of the futures trading system:

> **Bar → Regime → Router → Strategy.on_bar() → Decision**

The system uses a **regime-gated, priority-ordered, first-valid-signal routing mechanism**.

---

## 1. High-Level Architecture

```
Incoming Bar
     ↓
Regime Classification
     ↓
Strategy Candidate Routing (ordered)
     ↓
Sequential Strategy Evaluation
     ↓
First Valid Signal Wins (short-circuit)
     ↓
Execution Decision (TRADE / FLAT)
```

---

## 2. Regime Classification

Each incoming bar is classified into a regime using:

* ADX
* breakout_strength
* price_vs_vwap
* sqz_on
* volume_spike
* trend_strength
* bias
* pullback zone flags

### Priority Rules

```python
def classify_futures_bar_regime(bar) -> str:

    if bar.sqz_on and bar.adx < 30:
        return "SQUEEZE"

    if abs(bar.price_vs_vwap) >= 0.0035 and bar.in_pullback_zone:
        return "STRETCHED"

    if bar.adx >= 30 and bar.breakout_strength >= 0.60 and bar.bias != "NEUTRAL":
        return "TREND"

    if bar.adx >= 20 and bar.trend_strength >= 0.001 and bar.volume_spike >= 1.0:
        return "WEAK"

    return "WEAK"
```

### Regime Definitions

| Regime    | Meaning                                           |
| --------- | ------------------------------------------------- |
| SQUEEZE   | Low volatility compression, waiting for expansion |
| STRETCHED | Price extended from VWAP, mean reversion zone     |
| TREND     | Confirmed directional breakout                    |
| WEAK      | Weak directional pressure or choppy market        |

---

## 3. Router: Candidate Strategy Selection

Router determines **which strategies are allowed and in what order**.

```python
REGIME_CANDIDATES = {
    "TREND": ["active_strategy", "adaptive_orb"],
    "WEAK": ["active_strategy", "counter_vwap", "spring_upthrust", "kbar_feature"],
    "STRETCHED": ["active_strategy", "counter_vwap", "spring_upthrust"],
    "SQUEEZE": [],
}
```

### Active Strategy Resolution

```python
if name == "active_strategy":
    name = state.active_strategy_name
```

---

## 4. Strategy-Regime Compatibility

Each strategy defines its own regime filter in its configuration file:

```yaml
# Example from config/strategies/kbar_feature.yaml
regime_filter:
  allowed: ["weak", "bear", "down"]
  min_adx: 20
```

The router does NOT enforce regime compatibility at the routing level. Instead:
1. Each strategy internally checks if the current regime matches its `regime_filter`
2. If a strategy's regime filter doesn't match, it returns `None` (no signal)
3. The router continues to the next candidate strategy

This design allows strategies to have flexible regime definitions that may differ from the router's regime classification.

---

## 5. Core Routing Logic (Short-Circuit Execution)

```python
for name in candidates:

    strategy = registry.get(name)

    if strategy is None:
        notes.append(f"{name}: not registered")
        continue

    if prepare_strategy is not None:
        prepare_strategy(name, strategy)

    signal = strategy.on_bar(context)

    if signal is None:
        notes.append(f"{name}: no signal")
        continue

    is_valid, error = signal.validate()
    if not is_valid:
        notes.append(f"{name}: invalid signal ({error})")
        continue

    # FIRST VALID SIGNAL WINS
    return FuturesRouterDecision(
        action="TRADE",
        signal=signal,
        regime=regime,
        strategy=name,
        notes=notes,
    )
```

If no strategy produces a signal:

```python
return FuturesRouterDecision(
    action="FLAT",
    regime=regime,
    strategy=None,
    notes=notes,
)
```

---

## 6. Key Execution Principle

### First-Valid-Signal Wins

* Strategies are evaluated sequentially
* The first strategy returning a non-None signal is selected
* Remaining strategies are NOT evaluated

This is a **short-circuit routing model**

---

## 7. Strategy Trigger Sources

Each strategy has independent logic:

### counter_vwap (Mean Reversion)

* Trigger:

  * squeeze release (`fired=True`)
  * strong momentum
  * reversal confirmation or VWAP rejection
* Best regime: `STRETCHED`

---

### spring_upthrust

* Structural reversal pattern
* Counter-trend behavior
* Best regime: `STRETCHED`, `WEAK`

---

### kbar_feature (Multi-factor Momentum)

* Trigger:

  * bearish_align
  * ADX ≥ 20
  * close ≤ VWAP
  * score ≤ threshold
  * MACD < 0
  * momentum velocity < 0

* Best regime: `WEAK`, `BEAR`, `DOWN` (as defined in strategy's regime_filter)

---

## 8. Strategy Interaction Model

### Important Clarification

| Aspect                      | Behavior                        |
| --------------------------- | ------------------------------- |
| Signal generation           | Independent per strategy        |
| Cross-strategy coordination | None                            |
| Conflict resolution         | Router priority + short-circuit |
| Final execution             | Only one signal                 |

### Key Insight

> Strategies may produce conflicting signals internally,
> but router enforces a single outcome via priority ordering.

---

## 9. Strategy Starvation (Priority Shadowing)

Lower-priority strategies may rarely execute because:

* Earlier strategies frequently trigger signals
* Router stops evaluation early

Example:

```
[counter_vwap → spring_upthrust → kbar_feature]
```

If `counter_vwap` frequently fires:

* `kbar_feature` rarely gets evaluated

---

## 10. Attribution & Starvation Analysis System

The router now includes comprehensive attribution tracking to monitor strategy exposure and detect starvation.

### 10.1 AttributionRecorder

Located at `core/attribution_recorder.py`, this system logs:

1. **Router Evaluation Log** - Every candidate strategy evaluation
2. **Strategy Signal Log** - Signals generated by strategies
3. **Trade Attribution Log** - Trade execution with PnL attribution

### 10.2 Key Metrics

| Metric | Formula | Meaning |
|--------|---------|---------|
| Candidate Count | Count per strategy | How often strategy is considered |
| Evaluation Count | `evaluated=True` | How often strategy actually runs |
| Winner Count | `winner=True` | How often strategy wins |
| Shadowed Count | `status="shadowed"` | Times shadowed by higher priority |
| Starvation Index | `1 - (eval_count / candidate_count)` | 0.0=always evaluated, 1.0=never evaluated |
| Priority Impact | `shadowed_count / winner_count` | Suppression impact (higher=more suppressed) |

### 10.3 Starvation Levels

| Index Range | Level | Action |
|-------------|-------|--------|
| 0.0-0.3 | Acceptable | Monitor |
| 0.3-0.7 | Moderate | Consider priority adjustment |
| 0.7-1.0 | Severe | Priority adjustment needed |

### 10.4 Integration with Router

The router accepts an optional `recorder` parameter:

```python
def route_futures_signal(
    context: StrategyContext,
    recorder: AttributionRecorder | None = None
) -> dict | None:
```

All logging is guarded by `if recorder is not None:` to maintain backward compatibility.

### 10.5 Report Generation

Use the attribution report script:

```bash
python scripts/attribution_report.py --input-dir ./data/attribution --output-dir ./reports
```

Generates:
- `router_summary.csv` - Strategy exposure stats
- `starvation_report.csv` - Starvation analysis
- `priority_impact_report.csv` - Suppression analysis
- `trade_performance.csv` - PnL by strategy
- `merged_summary.csv` - Combined router + trade stats
- Visualizations (PNG charts)

### 10.6 Example Analysis

For `kbar_feature` (priority 2 in WEAK regime):
- Candidate count: 100
- Evaluated: 66 (shadowed 34 times)
- Starvation index: 0.34 (moderate)
- Priority impact: 1.7 (medium suppression)

This means `kbar_feature` was shadowed 34 times by higher-priority strategies (`counter_vwap`, `spring_upthrust`), winning only 20 times.

### 10.7 Usage in Production

1. **Enable attribution**: Pass `AttributionRecorder` instance to router
2. **Set output directory**: `recorder = AttributionRecorder(output_dir="./data/attribution")`
3. **Auto-flush**: Buffer size 1000 rows, flush interval 300 seconds
4. **Generate reports**: Daily or weekly analysis

---

## 11. Logging & Debugging (Critical)

Router must record evaluation trace:

### Recommended Notes Types

| Type              | Meaning                           |
| ----------------- | --------------------------------- |
| `no signal`       | Strategy evaluated but no trigger |
| `regime mismatch` | Filtered before evaluation        |
| `missing`         | Strategy not registered           |
| `winner`          | Selected strategy                 |
| `shadowed`        | Skipped due to higher priority win |
| `candidate`       | Initial candidate status          |

---

## 12. Example Execution (WEAK)

```
Regime: WEAK

Candidates:
[counter_vwap, spring_upthrust, kbar_feature]

Step 1:
counter_vwap → no signal

Step 2:
spring_upthrust → no signal

Step 3:
kbar_feature → SELL signal

Result:
TRADE via kbar_feature

Attribution Log:
- counter_vwap: evaluated, no_signal
- spring_upthrust: evaluated, no_signal  
- kbar_feature: evaluated, winner
```

---

## 13. Design Philosophy

This system is NOT:

* voting-based
* ensemble-based
* best-signal selection

This system IS:

> **Deterministic, priority-driven, regime-aware execution engine**

---

## 14. One-Line Definition

> **Regime-gated, priority-ordered, first-valid-signal routing system**

---

## 15. Future Extensions (Optional)

* Signal scoring & ranking (replace first-hit)
* Multi-strategy ensemble mode
* Dynamic priority adjustment based on attribution data
* Meta-router (learning-based)
* Real-time starvation monitoring dashboard

