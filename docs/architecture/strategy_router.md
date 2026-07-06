# Strategy Router

## Purpose

The strategy router sits between bar-level regime detection and trade execution. It decides **which strategies are allowed to speak** on each bar, evaluates them in priority order, and returns a single validated signal or an explicit no-trade decision.

---

## Regime → Strategy Mapping

Defined in `FuturesRouterConfig` (`core/futures_strategy_router.py`):

| Regime | Eligible Strategies | Rationale |
|---|---|---|
| **TREND** | `adaptive_orb` | Trend-following breakout. Only one strategy — directional markets need crisp triggers. |
| **WEAK** | `adaptive_orb`, `counter_vwap`, `spring_upthrust`, `kbar_feature`, `calendar_condor_v2` | Maximum strategy surface. Weak trend = more setups may fire. |
| **BEAR** | `counter_vwap`, `spring_upthrust` | Conservative short — countertrend reversals and upthrust fades only. Adaptive_orb intentionally excluded (breakout in bear = false breakout risk in low liquidity). |
| **STRETCHED** | `counter_vwap`, `spring_upthrust` | Mean-reversion only. No trend following in stretched extremes. |
| **SQUEEZE** | (none) | Wait for expansion confirmation. |
| **NO_DATA / UNKNOWN** | (none) | Bar data insufficient for classification. |

**Key design rule:** Strategies are NOT evenly distributed across regimes. Each regime has a specific profile of what's likely to work. Adding a strategy to the wrong regime (e.g., adaptive_orb in BEAR) creates false signals, not more trades.

---

## Strategy Priority

Within each regime, strategies are evaluated in tuple order. The **first strategy to return a valid Signal** wins — later candidates are shadowed.

For BEAR regime:
```
1. counter_vwap     → no signal? →
2. spring_upthrust  → no signal? →
                      → FLAT (no eligible signal)
```

If the active strategy (the one that last generated a trade) is still in the candidate list, it moves to position 0 for continuity.

---

## Strategy Eval Trace

Every `on_bar()` call produces a `StrategyEval` via `_set_eval()`:

```python
@dataclass
class StrategyEval:
    name: str
    enabled: bool
    triggered: bool
    action: str | None
    edge_score: float | None
    skip_reason: str | None
    notes: dict
```

The router collects all evals into a `RouterTrace` and:

1. **Prints one-line summary to stdout:**
   ```
   [RouterTrace] ts=2026-04-30 19:25 regime=BEAR selected=None |
   counter_vwap=SKIP:NO_FIRE_EVENT edge=0.0 |
   spring_upthrust=SKIP:NO_SQUEEZE edge=0.0
   ```

2. **Writes full JSONL to `logs/router_trace/router_trace_YYYYMMDD.jsonl`:**
   ```json
   {"ts": "...", "regime": "BEAR", "selected": null, "strategies": [
     {"name": "counter_vwap", "triggered": false, "skip_reason": "NO_FIRE_EVENT", ...}
   ]}
   ```

Every bar produces a trace, even no-trade bars. This is the primary tool for answering "why didn't we trade?".

---

## Counter-VWAP Skip Reasons

| Skip Reason | Meaning |
|---|---|
| `NO_FIRE_EVENT` | No recent squeeze activity AND no pending fire detection |
| `WEAK_FIRE` | Fire detected but |momentum| < min_momentum threshold |
| `FIRE_DETECTED_WAITING` | Fire just detected, waiting for confirmation bars |
| `FIRE_EXPIRED` | Fire exceeded confirm_bars without triggering |
| `NO_COUNTER_EXTREME` | Close hasn't established a new high/low — still trending with the fire |
| `VWAP_CONTEXT_INVALID` | Close broke the extreme but VWAP/momentum conditions aren't met |
| (triggered) | All conditions met → COUNTER_SELL / COUNTER_BUY |

---

## Spring-Upthrust Skip Reasons

| Skip Reason | Meaning |
|---|---|
| `NO_SQUEEZE` | `sqz_on` is False — squeeze required for spring/upthrust |
| `SPRING_CONTEXT_UNFAVORABLE` | Spring setup detected but context filter blocked it |
| `NO_PATTERN` | Squeeze active but no spring/upthrust pattern formed |
| (triggered) | SPRING (BUY) or UPTHRUST (SELL) |

---

## Calendar Condor V2 Skip Reasons

| Skip Reason | Meaning |
|---|---|
| `REGIME_NOT_WEAK` | Only trades in WEAK regime |
| `NIGHT_SESSION_DISABLED` | Night trading disabled by config |
| `ADX_TOO_HIGH` | ADX exceeds max_adx threshold (market too directional) |
| `SPREAD_STD_TOO_LOW` | Spread volatility too low for expected profit |
| `SPREAD_Z_NOT_EXTREME` | Neither spread_z nor vwap_z exceeded entry thresholds |
| (triggered) | LONG_SPREAD or SHORT_SPREAD entry |

---

## Healthy Silence

**No trade is a feature, not a bug.** The router is designed to stay flat when conditions don't match any strategy's trigger. Common healthy silence patterns:

- Night session: low liquidity, no squeeze → `NO_FIRE_EVENT` + `NO_SQUEEZE` = correct no-trade
- BEAR regime without squeeze: counter_vwap needs squeeze fire, spring_upthrust needs squeeze → both correctly skip
- Between regimes after a trend shift: strategies need 1-2 bars to reset state

Before investigating a no-trade period, check the RouterTrace dashboard in the Pipeline tab. The trace directly shows whether each strategy was evaluating correctly or blocked at the config/regime level.

---

## Related

- `docs/architecture/system_overview.md` — entry point
- `docs/operations/no_trade_diagnosis.md` — debug workflow
- `core/futures_strategy_router.py` — implementation
- `core/strategy_eval.py` — StrategyEval + RouterTrace dataclasses
