<!-- 2026-07-08 Gemini CLI: Finalize BB filter release plan with regime-based policy, minimum test checklist, and TDD implementation sequence -->
# Plan: Bollinger Band Filter for MTS Release

## Status

Proposed (2026-07-08) - Approved for TDD implementation.

## Background

Current release model:
```
ARMED → per-tick threshold check → threshold hit → PARTIAL_EXIT signal → MKP order
```

The release fires immediately when spread crosses the ATR stop. No filter on whether the
single-leg price is at a favorable level for execution.

## Proposal

Add an optional Bollinger Band filter AFTER the release threshold triggers.
BB does not decide *whether* to release — it optimizes *when* to release based on the current market regime.

```
threshold hit
    │
    ├── emergency bypass? (spread > 2× release stop)
    │     └── yes → release immediately (ignore BB)
    │
    ├── BB filter disabled → release immediately (current behavior)
    │
    └── BB filter enabled
          │
          ├── Regime: TREND → release immediately (bypass BB)
          │
          ├── Regime: STRETCHED → use loose BB filter (allowance × 1.5)
          │
          └── Regime: SQUEEZE / WEAK (or other) → use strict BB filter (standard allowance)
```

## Design Principles

1. **Release threshold is primary, BB is secondary.** BB never blocks release; it only
   delays it for a better execution price within a bounded window.

2. **BB operates on the single leg being released**, not on the spread.
   - If near leg is triggered: check near BB
   - If far leg is triggered: check far BB

3. **BB is a hard confirmation condition for spread release.** 
   - If BB does NOT confirm, the spread stays ARMED and re-evaluates on next tick.
   - No timeout forced release. Rationale: both legs are still present in the spread, meaning there is no naked leg exposure. Waiting for a better exit price does not create a time-bound risk of catastrophic loss, whereas a forced timeout release would transform "price optimization" into an arbitrary "time-based trigger."

4. **Regime-Based BB Policy.**
   To prevent being trapped in one-directional moves or expanding bands:
   - **TREND regime**: Bypass the BB filter entirely and release immediately (prevents trend stagnation).
   - **STRETCHED regime**: Apply a loose BB filter (multiply configured allowances `sell_within_bb_upper` / `buy_within_bb_lower` by `1.5` to allow exits near expanding bands under high volatility).
   - **SQUEEZE / WEAK regimes** (or fallback): Apply the strict configured BB allowances (reliable range boundaries).

5. **Emergency bypass overrides BB.** If spread has deteriorated beyond an emergency
   threshold (e.g. 2× normal release stop), ignore BB and release immediately.
   This bypass must set a flag rather than doing an early return, ensuring all lifecycle state updates, ledger logs, and state writing are properly handled.

6. **BB filter is optional and configurable per session.** Day/night sessions may
   have different BB parameters.

7. **BB computed on 5m bars, compared on every tick.**
   - BB bands recalculated once per 5m bar close (stable).
   - Each tick compares current price against cached bands (real-time).
   - Avoids tick-level BB noise from variable tick frequency.

8. **Distance-based confirmation, not position-based.**
   - SELL: `tick >= bb_upper - sell_within_bb_upper` (price near upper band)
   - BUY:  `tick <= bb_lower + buy_within_bb_lower` (price near lower band)
   - Config as absolute points (e.g. 8 pts), not relative (0.82).

9. **Single Source of Truth & Read-Only Context Compliance.**
   - The strategy must NEVER mutate the context `bar` dictionary (e.g., no writing of calculated bands into `bar`).
   - The calculated bands will be cached as strategy instance attributes (e.g., `self._near_bb_upper`).
   - Direct class attribute lookups are preferred over reflection (`getattr(self, ...)`) for performance and safety.

10. **Live vs. Backtest Data Pipeline Alignment.**
   - In backtesting, `df_5m` contains columns `near_close` and `far_close`.
   - In live trading, `monitor.py` gets separate deques (`_tick_bars_deque` and `_far_tick_bars_deque`).
   - To align these, `monitor.py`'s `_mts_tick()` will align the two deques and pass a unified `df_5m` DataFrame with columns `"near_close"` and `"far_close"` in `MarketData` before instantiating the context.

## BB Confirmation Logic

For the triggered leg, using 5m-bar cached BB bands:

| Release direction | BB confirm condition |
|---|---|
| BUY (cover short) | `price <= bb_lower + buy_within_bb_lower` |
| SELL (close long) | `price >= bb_upper - sell_within_bb_upper` |

Bands are recalculated on each 5m bar close from the last N bars of near/far close.
Between bar closes, the cached bands are compared against every tick.

## Configuration

```yaml
# config/futures_night.yaml (or futures.yaml)
mts:
  params:
    release_filter:
      bb_enabled: false                # default off; enable after backtest validation
      bb_period: 20                    # BB lookback bars (5m bars = ~100 min)
      bb_std_mult: 2.0                 # standard deviations for band width
      sell_within_bb_upper: 8          # pts: SELL allowed when tick within 8 pts of upper band
      buy_within_bb_lower: 8           # pts: BUY  allowed when tick within 8 pts of lower band
      emergency_bypass_enabled: true   # if spread exceeds 2× release stop, bypass BB
      emergency_bypass_mult: 2.0       # multiplier on normal release stop for emergency
```

### Architecture

```
5m bar close
    ↓
compute BB(near_close, period=20, std=2.0)  → cache self._near_bb_upper/lower
compute BB(far_close,  period=20, std=2.0)  → cache self._far_bb_upper/lower

each tick:
    spread > threshold?
        ↓ YES
    spread > emergency_threshold (2× release stop)?
        ↓ YES                              ↓ NO
    set bypass_bb = True             leg triggered?
        │                               ↓
        │                        Regime classification:
        │                           ├─ TREND ─────────→ bypass BB (release)
        │                           ├─ STRETCHED ─────→ loose BB filter (allowance × 1.5)
        │                           └─ SQUEEZE/WEAK ──→ strict BB filter
        ▼                               ↓
    release? ←─────────────────── YES (or bypass)
        ↓
   submit order & state transition
```

## Pre-Implementation Gate & Tests

You MUST write characterization tests first, before modifying any production code, to lock down the expected behavior and invariants.

### Minimum Test Checklist

1. **Threshold Hit + BB OK**
   - Assert `Signal` returned is `PARTIAL_EXIT`
   - Assert `release_group.status` transitions to `ReleaseGroupStatus.TRIGGERED`
   - Assert `_released_leg` is populated correctly.

2. **Threshold Hit + BB Not OK (SQUEEZE / WEAK regime)**
   - Assert return is `None` (strategy stays ARMED, waiting for confirmation)
   - Assert `_released_leg` is NOT written (remains `None`)
   - Assert `release_group.status` remains `ARMED` (does not transition to `TRIGGERED` prematurely)
   - Assert `_set_eval` sets skip reason to `"BB_FILTER_WAITING"`.

3. **Threshold Hit + BB NaN / Unavailable**
   - Assert it bypasses the BB filter and releases immediately (returns `PARTIAL_EXIT` Signal)
   - Assert it does not enter a deadlock or stay ARMED forever.

4. **Emergency Bypass**
   - Assert that if the loss exceeds the emergency bypass threshold, it ignores the BB filter and releases immediately.
   - Assert it does NOT early return in a way that skips state updates; it must go through the full commit path, and the state and state file are correctly updated.

5. **Regime-Based Policy Verification**
   - **TREND regime**: Assert that if regime is `"TREND"`, the strategy bypasses the BB filter and releases immediately even if the price is far from the BB bands.
   - **STRETCHED regime**: Assert that if the regime is `"STRETCHED"`, it uses the loose BB filter (allowance multiplied by 1.5) and confirms the release at a wider distance than strict mode.

6. **Live `df_5m` Column Alignment**
   - Assert that [monitor.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/strategies/futures/monitor.py)'s `_mts_tick()` aligns the near/far deques into a single `df_5m` containing columns `"near_close"` and `"far_close"` before passing it.

## Implementation Sequence (TDD Waves)

To keep changes clean, surgical, and robust, execute the implementation in the following sequence:

* **Wave 1: Add Characterization Tests (Task 1)**
  - Implement tests matching the **Minimum Test Checklist** (tests 1 to 5) in [test_tmf_spread_atr.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/tests/strategies/test_tmf_spread_atr.py).
  - Verify that the new tests fail (proving the tests can fail and the feature is not yet implemented).

* **Wave 2: Configuration updates (Task 2)**
  - Add parameters to config files and initialize them in strategy `__init__` and hot-reload.

* **Wave 3: Monitor Data Alignment (Task 3)**
  - Implement the deque alignment and passing of `df_5m` inside `_mts_tick` in [monitor.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/strategies/futures/monitor.py).
  - Implement test 6 to verify alignment works correctly.

* **Wave 4: Cache Bands as Strategy Attributes (Task 4)**
  - Compute the Bollinger Bands in `on_bar` using the aligned `df_5m` and save them to class variables.

* **Wave 5: Implement Filter Checks and Deadlock Prevention (Task 5 & 6)**
  - Implement `_check_bb_filter` using direct attribute access and NaN safeguards, incorporating the regime-based policy.
  - Modify `_commit_action` to prevent premature status mutation.
  - Integrate BB checks and the flag-based emergency bypass inside `_manage_position`.

* **Wave 6: Telemetry and Logging (Task 7)**
  - Add `_set_eval` logs, assertions, and state file output validation.
  - Verify that all characterization tests pass.

---

### New method: `_check_bb_filter(bar, release_leg, regime) -> bool`

In `tmf_spread.py`:

```python
def _check_bb_filter(self, bar: dict, release_leg: Leg, regime: str = "NEUTRAL") -> bool:
    """Check if the triggered leg's price is near the favorable BB band.
    
    Uses cached BB bands stored as strategy attributes. Returns True if current 
    tick price is within the configured distance of the target band.
    
    Regime-based policy:
    - TREND: bypass BB filter entirely
    - STRETCHED: loosen BB allowance by 1.5x
    - SQUEEZE / WEAK / others: apply strict configured BB allowance
    """
    if not self._bb_enabled:
        return True  # no filter → always confirm

    regime_upper = str(regime).upper()
    if "TREND" in regime_upper:
        # Strong trend: bypass BB to prevent trend stagnation/getting trapped
        return True
    
    close_key = "near_close" if release_leg == Leg.NEAR else "far_close"
    price = bar.get(close_key, 0)
    
    # Direct class attribute lookup (prevents mutating context + avoids reflection)
    if release_leg == Leg.NEAR:
        bb_upper = self._near_bb_upper
        bb_lower = self._near_bb_lower
        side = self._near_side
    else:
        bb_upper = self._far_bb_upper
        bb_lower = self._far_bb_lower
        side = self._far_side
    
    # BB bands not computed or stale/NaN → fallback to immediate release (robust NaN check)
    if not (bb_upper > 0) or not (bb_lower > 0) or pd.isna(bb_upper) or pd.isna(bb_lower):
        return True  # no BB data, don't block release
    
    # Apply regime-based allowance multipliers
    sell_allowance = self._sell_within_bb_upper
    buy_allowance = self._buy_within_bb_lower
    if "STRETCHED" in regime_upper:
        sell_allowance *= 1.5
        buy_allowance *= 1.5
    
    if side == "SHORT":
        # Covering short → want to BUY at low price (near lower band)
        return price <= bb_lower + buy_allowance
    else:
        # Closing long → want to SELL at high price (near upper band)
        return price >= bb_upper - sell_allowance
```

### Integration in `on_bar()` / `_manage_position()`

In the release decision section (around L1981):

```python
# After threshold is confirmed:
if _decision.action == LifecycleAction.RELEASE:
    _release_leg = _decision.release_leg
    
    # ── Emergency bypass check ──
    _current_pnl = max(abs(_n_pnl), abs(_f_pnl))
    _emergency_threshold = release_stop * self._emergency_bypass_mult
    _bypass_bb = False
    if self._emergency_bypass_enabled and _current_pnl > _emergency_threshold:
        logger.warning(f"🚨 [BB_EMERGENCY_BYPASS] spread PnL={_current_pnl:.0f} > {_emergency_threshold:.0f} — bypassing BB filter")
        _bypass_bb = True
            
    # ── BB filter check ──
    if not _bypass_bb:
        _regime = context.market.regime if hasattr(context.market, "regime") else "NEUTRAL"
        if not self._check_bb_filter(bar, _release_leg, regime=_regime):
            self._set_eval(skip_reason="BB_FILTER_WAITING", leg=_release_leg.value, price=near_close if _release_leg == Leg.NEAR else far_close)
            return None
            
    # BB confirmed/bypassed → proceed with release
    # ── Defer status transition to prevent deadlock if price retraces ──
    # Note: Modify _commit_action so LifecycleAction.RELEASE does NOT transition to TRIGGERED immediately.
    # We transition it here exactly when we commit to returning the Signal.
    self._lifecycle_oca.release_group.status = ReleaseGroupStatus.TRIGGERED
    
    # (standard release execution code follows...)
```

### State tracking

Add to strategy:
```python
# Configuration parameters
self._bb_enabled: bool = False
self._bb_period: int = 20
self._bb_std_mult: float = 2.0
self._sell_within_bb_upper: float = 8.0
self._buy_within_bb_lower: float = 8.0
self._emergency_bypass_enabled: bool = True
self._emergency_bypass_mult: float = 2.0

# Indicator cache
self._near_bb_upper: float = 0.0
self._near_bb_lower: float = 0.0
self._far_bb_upper: float = 0.0
self._far_bb_lower: float = 0.0
```

Load configuration parameters under the `release_filter` block inside `__init__` and the hot-reload section of `on_bar()`:
```python
_filter_cfg = _params.get("release_filter", {})
self._bb_enabled = bool(_filter_cfg.get("bb_enabled", self._bb_enabled))
self._bb_period = int(_filter_cfg.get("bb_period", self._bb_period))
self._bb_std_mult = float(_filter_cfg.get("bb_std_mult", self._bb_std_mult))
self._sell_within_bb_upper = float(_filter_cfg.get("sell_within_bb_upper", self._sell_within_bb_upper))
self._buy_within_bb_lower = float(_filter_cfg.get("buy_within_bb_lower", self._buy_within_bb_lower))
self._emergency_bypass_enabled = bool(_filter_cfg.get("emergency_bypass_enabled", self._emergency_bypass_enabled))
self._emergency_bypass_mult = float(_filter_cfg.get("emergency_bypass_mult", self._emergency_bypass_mult))
```

### BB indicator computation

Add to strategy `on_bar()` before position management checks (uses the unified `df_5m` passed from `monitor.py`):

```python
df_5m = context.market.df_5m
if df_5m is not None and not df_5m.empty and len(df_5m) >= self._bb_period:
    near_close_series = df_5m["near_close"].rolling(self._bb_period)
    far_close_series = df_5m["far_close"].rolling(self._bb_period)
    
    # Store on strategy instance (prevents read-only context violation)
    near_mid = near_close_series.mean().iloc[-1]
    self._near_bb_upper = near_mid + self._bb_std_mult * near_close_series.std().iloc[-1]
    self._near_bb_lower = near_mid - self._bb_std_mult * near_close_series.std().iloc[-1]
    
    far_mid = far_close_series.mean().iloc[-1]
    self._far_bb_upper = far_mid + self._bb_std_mult * far_close_series.std().iloc[-1]
    self._far_bb_lower = far_mid - self._bb_std_mult * far_close_series.std().iloc[-1]
else:
    self._near_bb_upper = self._near_bb_lower = 0.0
    self._far_bb_upper = self._far_bb_lower = 0.0
```

## Risks

1. **BB bands too wide** → never confirms, effectively disabled. Mitigated by `emergency_bypass_mult` and conservative configuration.
2. **Additional computation per tick** → BB rolling mean/std on ~20 bars is negligible.
3. **Overfitting** → BB period/mult optimized on backtest may not hold live. Use conservative defaults (period=20, std=2.0) and validate out-of-sample.
4. **BB unavailable / stale** → if bar data hasn't arrived or bands failed to compute, fallback to immediate release (do NOT wait). Handled by NaN check.
5. **Threshold direction changed while waiting** → if the triggered release side flips while waiting for BB confirmation, the strategy automatically evaluates the new triggered side on the next tick since no timeout state is retained.
