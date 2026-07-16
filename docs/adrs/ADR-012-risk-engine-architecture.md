# ADR-012: MTS Decoupled Risk Engine & Layered Architecture

- **Date**: 2026-07-14
- **Author**: Gemini CLI
- **Status**: Accepted

## Context

The current MTS (TMF spread strategy) implementation in [tmf_spread.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/strategies/plugins/futures/active/tmf_spread.py) suffers from state modification coupling. Volatility (ATR), VWAP, profit locking, and trend momentum all attempt to directly modify the strategy's `trail_dist` and `release_stop` variables at different execution points. This ad-hoc cascading modification creates hidden state interactions, increases the risk of logical conflicts, and makes the strategy hard to backtest, verify, or scale.

To support adding future factors (such as MTF trends, Order Flow, DOM, VPIN, CVD) without rotting the core execution lifecycle, we need to transition to a clean, decoupled **Layered Architecture** with **Unified Risk Engines**.

## Decision

We will refactor the MTS strategy risk path into two separate decoupled engines:
- **`ReleaseRiskEngine`**: Manages the spread phase (two legs open) where risk is related to spread widening, leg-specific PnLs, and Bollinger Band filters.
- **`SingleLegRiskEngine`**: Manages the single-leg phase (one leg remaining after release) where risk is related to trailing stop tightening and real-time price invalidations.

```
                    ┌─────────────────────────┐
                    │     Indicator Layer     │ (ATR, VWAP, BB, MTF)
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │      Feature Layer      │ (Volatility, Deviation, MTF Score)
                    └────────────┬────────────┘
                                 │
            ┌────────────────────┴────────────────────┐
            ▼                                         ▼
┌───────────────────────┐                 ┌───────────────────────┐
│   ReleaseRiskEngine   │                 │  SingleLegRiskEngine  │
│ (Evaluates Spread Leg │                 │ (Evaluates Single Leg │
│     Release Stops)    │                 │   Trailing & Exits)   │
└───────────┬───────────┘                 └───────────┬───────────┘
            │                                         │
            └────────────────────┬────────────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │  Release/Trail Engine   │ (Execution state machine checks)
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │       Exit Layer        │ (Dispatches orders via Shioaji)
                    └─────────────────────────┘
```

### 1. Data Structures

We define strict inputs and outputs for both engines to prevent ad-hoc global state mutation:

```python
@dataclass(frozen=True)
class ReleaseRiskInput:
    base_release_stop_pts: float
    near_pnl: float
    far_pnl: float
    spread: float
    spread_atr: Optional[float]
    bb_squeeze_on: bool
    tick_confirmed: bool

@dataclass(frozen=True)
class SingleLegRiskInput:
    side: str
    current_price: float
    entry_price: float
    peak_price: float
    base_trail_dist_pts: float
    atr_used: Optional[float]
    vwap: Optional[float]
    mtf_score: Optional[float]
    mtf_valid: bool
    mtf_age_sec: Optional[float]
    unrealized_pnl: float
    mfe_pts: float = 0.0

@dataclass(frozen=True)
class RiskDecision:
    base_value: float
    final_value: float
    modifiers: tuple[str, ...]
    exit_candidate: bool
    exit_reason: Optional[str] = None
    shadow_final_value: Optional[float] = None
    shadow_modifiers: tuple[str, ...] = ()
```

---

### 2. SingleLegRiskEngine: 4-Layer Execution Model

The `SingleLegRiskEngine` evaluates risk across four distinct, isolated layers in order of precedence:

1.  **Baseline Layer**:
    *   Generates base unadjusted parameters from fixed points, ATR multipliers, warmup defaults, and session configs (`base_trail_dist_pts`).
2.  **Hard Constraint Layer**:
    *   Applies bounds and limits that cannot be relaxed by subsequent layers, such as `Profit Lock` maximum giveback limits and hard floors (`max_trail_floor = 20.0`).
3.  **Market Structure Layer**:
    *   Applies real-time structural price levels. **VWAP** resides in this layer and acts as a formal execution modifier:
        *   If the price violates VWAP, it formally tightens `trail_dist` by the configured ratio (down to a floor of `5.0` pts) and adds `"VWAP_EXIT_TIGHTENED"` to formal `modifiers`.
4.  **Context Layer**:
    *   Adjusts confidence based on multi-timeframe regime alignment. **MTF** resides in this layer.
    *   **Price Triggers, MTF Adjusts**:
        *   LONG holding with MTF strong bearish (conflict): Tightens stop.
        *   LONG holding with MTF strong bullish: Retains baseline.
        *   LONG holding with MTF stale: Ignore.
    *   **Phase 2 Invariant**: MTF context is evaluated in **Shadow Mode** only. It will calculate `shadow_final_value` and log `shadow_modifiers` but **must not** modify the formal `final_value` passed to the order dispatcher.

---

### 3. Experimental Design & Counterfactual Logging

To objectively verify MTF's predictive value compared to VWAP, we evaluate four counterfactual variants:
- **Baseline**: ATR + Profit Lock
- **VWAP**: Baseline + VWAP
- **MTF**: Baseline + MTF shadow
- **VWAP + MTF**: Baseline + VWAP + MTF shadow

During the single-leg phase, we output three levels of structured logs on every tick depending on the comparison:
- `[MTS_MTF_SHADOW_EVAL]`: Logs general state details (`side`, `score`, `baseline_trail`, `formal_trail`, `shadow_trail`, `formal_modifiers`, `shadow_modifiers`, `delta_pts`).
- `[MTS_MTF_SHADOW_DIFF]`: Logs when MTF shadow is tighter than formal (`shadow_trail < formal_trail`).
- `[MTS_MTF_SHADOW_NO_CHANGE]`: Logs when MTF shadow is equal to formal (`shadow_trail == formal_trail`).

When a single-leg position is exited, we output a consolidated trade-level summary:
- `[MTS_MTF_SHADOW_TRADE_SUMMARY]`:
  - `trade_id`: Unique trade identifier.
  - `formal_exit_price`: Price at formal exit.
  - `formal_realized_pnl`: Realized PnL of formal execution.
  - `shadow_trigger_ts`: First timestamp where `shadow_trail` would have triggered an exit.
  - `shadow_trigger_price`: Price at shadow trigger timestamp.
  - `shadow_hypothetical_pnl`: Hypothetical PnL if exited at shadow trigger.
  - `formal_max_giveback`: Maximum profit giveback from peak under formal execution.
  - `shadow_max_giveback`: Maximum profit giveback from peak under shadow execution.
  - `shadow_would_exit_earlier`: Boolean indicating if shadow would have exited before the formal path.
  - `post_shadow_mfe`: Max Favorable Excursion after shadow trigger.
  - `post_shadow_mae`: Max Adverse Excursion after shadow trigger.

---

## Implementation Roadmap

### Phase 1: MTF Mode & Caching [Completed]
- Config block `mts.mtf.mode: shadow` / `max_age_sec: 420` established.
- `MtfSnapshot` dataclass and caching implemented in `monitor.py` on 5m bar completion.
- Tick-level injection and freshness checking completed. All unit tests passed.

### Phase 2: Decoupled Risk Engines & Shadow MTF [Completed]
- Define `ReleaseRiskEngine` and `SingleLegRiskEngine` in `strategies/plugins/futures/active/risk_engine.py`.
- Refactor `tmf_spread.py` to instantiate and route parameter queries through these engines.
- Implement structured shadow logs and trade summaries.
- Implement golden verification test suite to prove behavior identity before and after refactoring.

### Phase 3: MTF Shadow to Enabled
- Based on paper trading stats, transition MTF from `shadow` mode to `enabled` mode, allowing context adjustments to formally dictate the Risk Engine's execution outputs.

---

## Operational Telemetry & State-Restore Contracts

To prevent desyncs and visual desensitization (such as UPL frozen at 0 TWD or missing trade summaries), the following telemetry contracts must be strictly preserved:

1. **State-Restore Recovery Invariant**:
   * The strategy's `self._mts_recovery_state` is initialized to `RecoveryState.INITIALIZING` during startup.
   * In-memory status is restored at the top of `on_bar()` from the JSON state file or fill logs.
   * **Rule**: Once state recovery successfully completes, `self._mts_recovery_state` must be immediately updated to `RecoveryState.RECOVERED` (or `RecoveryState.FLAT_CONFIRMED` if flat).
   * **Rationale**: The monitor uses this status to guard the heartbeat telemetry. If it remains `INITIALIZING`, the monitor operates in a degraded mode (telemetry-only, suppressed lifecycle updates) and defaults all UPL calculations to `0.0` points.

2. **Hot-Reloading Execution Constraints**:
   * Execution safety filters like `max_quote_age_ms`, `max_spread_width`, `confirm_ticks`, and `confirm_ms` must be hot-reloaded dynamically from the strategy context on each tick in `on_bar()`.
   * This allows real-time adjustment of quote age thresholds (e.g. relaxing to `2000` ms) to adapt to shifting market environments without requiring a PM2 restart.

3. **Log Completeness Guarantee**:
   * Every single-leg exit (whether triggered on tick start by `evaluate_lifecycle_actions` or dynamic intra-bar extremes in fallback paths) must call `self._log_shadow_trade_summary` prior to returning the exit signal.
   * This ensures 100% telemetry capturing for counterfactual shadow MTF comparison metrics.
