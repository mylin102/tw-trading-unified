# GSD Plan: IV Skew Pipeline (from ref_250516.md 第2章)

## Goal

Realize ref_250516.md Section 2 in code: classify IV curve shape transformations
(parallel shift / left-skew / right-skew) from live Shioaji bidask stream, and
inject a regime-level skew signal into the strategy layer.

## Current State

Already wired:
- `core/derivatives/surface_engine.py` — receives OptionQuoteEvent from `main.py` bidask_dispatcher
- `main.py:229-249` — ATM C/P quotes → OptionQuoteEvent → surface_engine.on_quote()
- `main.py:261-285` — OTM skew contracts → OptionQuoteEvent → surface_engine.on_quote()
- `main.py:358-459` — _subscribe_otm_skew_contracts() subscribes OTM_P and OTM_C at ±300 pts
- Skew signal currently uses **put_call_divergence only** — no IV surface shape analysis

What ref_250516.md Section 2 adds that we don't have:
1. Multi-strike IV sampling (need 3-5 strikes per side, not just 2)
2. Curve shape classification (parallel / left-skew / right-skew)
3. Velocity tracking (how fast the shape changed)

## Architecture Decisions

1. **3-strike Phase 1 only** — OTM Put / ATM / OTM Call is already subscribed
   and wired. No additional subscription load. 5-strike (near-ATM wings) deferred
   to Phase 2.5.

2. **BS IV calculator required** — raw premium is not comparable across strikes
   (same premium means different IV for 33000 vs 34000). Need IV as common
   basis for shape analysis.

3. **Skew shape formula**: bounded normalized difference (not ratio):

   ```
   put_slope = otm_put_iv - atm_iv
   call_slope = otm_call_iv - atm_iv
   slope_ratio = (call_slope - put_slope) / (abs(call_slope) + abs(put_slope) + eps)
   ```

   Range: strictly [-1, 1]. Never blows up on zero call_slope.

## Task List

### Phase 1: IV Curve Shape Classification Engine

Execution order per user approval:
1. Task 1 (IV calculator) — foundation, no dependencies
2. Task 3 (shape_classifier) — pure logic, testable with synthetic data
3. Task 2 (surface_engine snapshot) — depends on Task 1 for IV computation
Then Phase 2:
4. Task 4 (shared_state injection)
5. Task 5 (theta_gang adaptation)
6. Task 6 (strategy_router)

#### Task 1 — Implied Volatility Calculator

**Description**: Add a lightweight Black-Scholes IV calculator to
`core/derivatives/` that takes (option_type, strike, price, underlying, expiry)
and returns implied volatility. Use numerical root-finding (Newton or
bisection). This is the prerequisite for all IV surface work.

**Acceptance Criteria**:
- [ ] `core/derivatives/iv_calculator.py` with `iv_from_price(opt_type, strike, premium, underlying, dte) → float`
- [ ] Bisection method converges within 1e-6 tolerance in < 50 iterations
- [ ] Handles edge cases: deep ITM (IV floor), zero premium, expired contracts
- [ ] `test_iv_calculator.py` passes with known test cases

**Files Likely Touched**:
- `core/derivatives/iv_calculator.py` (NEW)
- `core/derivatives/__init__.py`
- `tests/test_iv_calculator.py` (NEW)

**Estimated Scope**: S (2 files)

---

#### Task 2 — Multi-Strike IV Curve Sampler

**Description**: Upgrade surface_engine to compute IV from ALL strikes in the
quote_store, not just the two OTM points. Store a surface snapshot:
```
{
  (CALL, 33000): {mid, iv, timestamp},
  (PUT, 33000): {mid, iv, timestamp},
  ...
}
```
Add a method `surface_snapshot(futures_price) → dict` that returns the current
IV curve as structured data.

**Acceptance Criteria**:
- [ ] On every `compute_if_ready()`, all stored quotes get IV computed
- [ ] `surface_snapshot()` returns `{calls: {strike: iv, ...}, puts: {strike: iv, ...}, atm: float, dte: float}`
- [ ] Existing SkewSignal output is unchanged (backward compatible)
- [ ] Cooldown applies to both IV computation and skew computation

**Files Likely Touched**:
- `core/derivatives/surface_engine.py`
- `core/derivatives/models.py` (maybe add SurfaceSnapshot model)

**Estimated Scope**: M (3 files)

---

#### Task 3 — IV Curve Shape Classifier

**Description**: Implement the classification logic from ref_250516.md Section 2:

1. **Parallel shift**: All IVs lifted uniformly (look at ATM IV change)
2. **Left-skew (逆時針)**: Put wing IV slope increased more than call wing
3. **Right-skew (順時針)**: Call wing IV slope increased more than put wing

Algorithm:
- Compute IV slope ratio: (OTM_put_iv - ATM_iv) / (OTM_call_iv - ATM_iv)
- Normalize to [-1, 1] range where 0 = neutral
- Track delta from previous to get velocity

Output: `SkewRegime` with fields:
- `shape`: "PARALLEL" | "LEFT_SKEW" | "RIGHT_SKEW" | "NEUTRAL"
- `slope_ratio`: float (-1 to 1, negative = left-skew)
- `delta_slope_ratio`: float (velocity)
- `atm_iv_change`: float (parallel shift magnitude)
- `confidence`: float

**Acceptance Criteria**:
- [ ] Classifier returns correct shape for synthetic test cases:
  - LEFT_SKEW: put_slope >> call_slope → slope_ratio < -threshold
  - RIGHT_SKEW: call_slope >> put_slope → slope_ratio > +threshold
  - PARALLEL: both slopes positive, near equal → slope_ratio ≈ 0, atm_iv_change high
  - NEUTRAL: both slopes near zero → slope_ratio ≈ 0, atm_iv_change low
- [ ] `slope_ratio` is strictly in [-1, 1] range, never blows up
- [ ] `delta_slope_ratio` tracks change from previous snapshot
- [ ] Output serializable to dict for shared_state injection

**Files Likely Touched**:
- `core/derivatives/shape_classifier.py` (NEW)
- `core/derivatives/models.py` (add SkewRegime dataclass)
- `tests/test_shape_classifier.py` (NEW)

**Estimated Scope**: M (3 files)

---

### Checkpoint 1

- [ ] `test_iv_calculator.py` passes
- [ ] `test_shape_classifier.py` passes
- [ ] Manual: Start system in paper mode, check logs show IV values and shape
- [ ] No regression: existing SkewSignal still printed correctly

---

### Phase 2: Skew Regime Pipeline Injection

#### Task 4 — Wire SkewRegime Into main.py Loop

**Description**: In the main trading loop (`main.py`), call the shape classifier
periodically (not every tick — once per bar is enough) and inject the
`SkewRegime` into shared_state so strategy layer can consume it.

Integration point: after `compute_if_ready()`, call `classify(surface_snapshot)`
and write result to `fm.shared_state["skew_regime"]`.

**Acceptance Criteria**:
- [ ] `main.py` calls shape classifier after every `compute_if_ready()`
- [ ] `fm.shared_state["skew_regime"]` contains dict with shape, slope_ratio, confidence
- [ ] `shared_state` serialization for dashboard JSON export includes skew_regime
- [ ] Log line printed: `[SkewRegime] shape=LEFT_SKEW slope=-0.42 confidence=0.73`

**Files Likely Touched**:
- `main.py`
- Maybe `core/shared_state.py` if it exists

**Estimated Scope**: S (1-2 files)

---

#### Task 5 — Adapt ThetaGang with Skew Regime Awareness

**Description**: Currently `theta_gang.py` selects strikes based on fixed
parameters (wing_width=200, otm_offset=200). Add skew regime awareness:

- LEFT_SKEW (fear): Widen put wing, tighten call wing
- RIGHT_SKEW (euphoria): Widen call wing, tighten put wing
- PARALLEL (tension): Reduce position size or skip entirely
- NEUTRAL: Use default parameters

**Acceptance Criteria**:
- [ ] `theta_gang.py::select_strikes()` accepts optional `skew_regime` parameter
- [ ] LEFT_SKEW → put wing +50%, call wing -50% offset adjustment
- [ ] RIGHT_SKEW → call wing +50%, put wing -50% offset adjustment
- [ ] PARALLEL → scale position down to 50% (or skip logic)
- [ ] No change to existing behavior when skew_regime is None

**Files Likely Touched**:
- `strategies/options/theta_gang.py`

**Estimated Scope**: S (1 file)

---

### Checkpoint 2

- [ ] System log shows `[SkewRegime]` lines during market hours
- [ ] ThetaGang strike selection varies with skew regime
- [ ] Dashboard shows skew_regime if exported
- [ ] Night session: skew_regime = UNKNOWN (no option data) → theta_gang uses defaults

---

### Phase 3: Six-Category Strategy Router (ref_250516.md Section 3)

#### Task 6 — Strategy Regime Mapper

**Description**: Implement a mapping from (skew_regime, underlying_trend) to
one of the six strategy categories from ref_250516.md:

| Skew Regime | Trend | Suggested Category |
|---|---|---|
| LEFT_SKEW | Down | Bearish (Bear Put Spread) |
| LEFT_SKEW | Neutral | Hedging (Protective Put) |
| RIGHT_SKEW | Up | Bullish (Bull Call Spread) |
| RIGHT_SKEW | Neutral | Income (Wheel) |
| PARALLEL | any | Volatility (Straddle/Strangle) |
| NEUTRAL | any | Neutral/Range (Iron Condor) |

This is a **router** — it outputs a recommendation, not an execution.

**Acceptance Criteria**:
- [ ] `core/derivatives/strategy_router.py` with `route(skew_regime, trend) → str`
- [ ] All 6 categories are reachable from some input combination
- [ ] Returns "UNKNOWN" when no good match (e.g. LEFT_SKEW + strong UP)
- [ ] Pure logic, no side effects — trivially testable

**Files Likely Touched**:
- `core/derivatives/strategy_router.py` (NEW)
- `tests/test_strategy_router.py` (NEW)

**Estimated Scope**: S (2 files)

---

### Checkpoint 3

- [ ] `test_strategy_router.py` passes all 8+ combinations
- [ ] Full pipeline: IV data → shape classifier → regime → strategy recommendation
- [ ] Manual review of router decisions with human

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| OTM strike subscription fails (no bidask) | H | Subscribe 3 strikes, if OTM unavailable fallback to ATM-only (NEUTRAL output) |
| BS IV calculator inaccurate for deep OTM | M | Use bisection, cap iterations at 100, validate against known market data |
| Night session: no option data | M | SkewRegime = UNKNOWN, theta_gang uses defaults (already handled) |
| Bidask callback frequency overwhelms IV calc | M | Cooldown already in surface_engine (5s), shape classifier runs at same cadence |

## Open Questions

1. Strike_rounding: TXO tick increment is 50 pts below 10000 and 100 pts above.
   Currently surface_engine uses otm_points=300. Need to verify this resolves
   actual strikes for the current TXO regime (index ~34000, step=100).
   → Confirmed from `main.py:358-459` code: inferred_step is computed from
   actual contract chain, OTM targets use `_nearest_strike()`.

2. Should shape classifier run on every compute_if_ready() or on a separate
   timer? → On every compute_if_ready() is fine because surface_engine already
   has a 5s cooldown.

## Plan Status

Created: 2026-05-17
Status: Awaiting approval before execution
