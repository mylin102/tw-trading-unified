# R-004: MTS Entry Feature Dependency Audit

**Status:** Accepted — baseline, not superseding any prior document
**Supersedes:** None
**Superseded by:** None (future: R-007 instrument, R-009 evaluate, R-011 integrate)
**Start Date:** 2026-07-22
**Evidence Level:** E3 (Verified by codebase trace, confirmed by domain review)
**References:** ADR-006 (MTS Risk Decomposition), ADR-009 (Position Lifecycle OCA),
  `tmf_spread.py`, `calendar_condor_v2.py`, `kbar_feature.py`
**Upstream Enquiry:** Hermes Agent initial analysis of futures page parameter relevance to MTS entry
**Scope boundary:** This audit establishes static decision dependency, not runtime path
  coverage. Absence of a code dependency does not prove absence of indirect upstream
  data influence. In particular, `spread_z` itself is produced by upstream transformations
  (spread_loader CSV pipeline) that may consume fields not traced here; this document
  proves the MTS consumer boundary, not the full feature-generation lineage.
**Valid at:** git commit pending (file created 2026-07-22), repo state clean,
  audited files: `tmf_spread.py` L1–L3276 (r3276), `calendar_condor_v2.py` L1–L545,
  `kbar_feature.py` L1–L395. Re-audit when MTS entry path is modified.

---

## Problem

The futures dashboard computes ~65 parameters per ticker, grouped into Squeeze, Trend,
MeanRev, ML/Feature, and Router categories. The question: which of these parameters
participate in MTS (tmf_spread) entry decision, and which should be excluded?

A preliminary analysis correctly identified spread_z as the primary signal, but
incorrectly attributed several behaviours from other strategies to MTS. This research
note establishes a verified dependency map and documents the misattributions.

---

## Out of Scope

This audit does **not** evaluate:

- statistical usefulness, predictive power, or feature importance of any parameter
- feature interactions (e.g. whether regime × ADX × spread_z would predict reversion)
- execution quality, slippage, or fill latency
- parameter optimization or threshold tuning
- counterfactual performance of any excluded feature
- live trading effectiveness

**Correct interpretation of this document:**

- `"ADX is not consumed by MTS entry"` ✓ — verifiable from code
- `"ADX is useless for MTS"` ✗ — not evaluated here

This distinction matters: a feature not consumed today may become valuable
after instrumentation reveals an association.

---

## Verified Facts

Each entry traces `producer → consumer → decision branch → downstream effect`.

### spread_z

| Attribute | Detail |
|---|---|
| Producer | `spread_loader` → `mxf_calendar_spread_*.csv` |
| Consumer | `tmf_spread.on_bar()` L2129 |
| Decision branch | `if abs(spread_z_f) < self._entry_z: return None` |
| Downstream effect | Entire entry pipeline blocker |
| **Role** | **Sole market signal gate** |
| **Not role** | Direction (derived from sign), quantity, timing |
| **Verification basis** | `source_file: tmf_spread.py, symbol: on_bar, lines: 2105-2131` — negative search: `rg "spread_z" tmf_spread.py` confirms no other decision branch consumes spread_z; `rg "entry_z\|entry_z_cfg" tmf_spread.py` confirms threshold is the only filter |

### ATR

| Attribute | Detail |
|---|---|
| Producer | futures page computation (`Atr`, `Atr 5`, `Atr 10`, `Atr 20`, `Atr 60`) |
| Consumer | `tmf_spread.on_bar()`: L2090 (staleness), L2117 (threshold regime), L976-994 (stop/trail) |
| Decision branches | `if atr < self._min_atr: return None` (staleness gate); dynamic entry_z selection (2.0/2.5/3.0) |
| Downstream effect | Entry blocked below min ATR; threshold widened under high ATR |
| **Role** | **Threshold regime selector, staleness gate, post-entry risk input** |
| **Not role** | Position sizing (MTS uses fixed 1 contract — confirmed `tmf_spread.py` L2133-2135 has no quantity calculation) |
| **Verification basis** | `source_file: tmf_spread.py, symbol: on_bar` — threshold regime: L2116-2127; staleness gate: L2090-2093; stop/trail: `_get_thresholds()` L974-1000 |

### near_close / far_close

| Attribute | Detail |
|---|---|
| Producer | tick/bar market data |
| Consumer | `tmf_spread.on_bar()` L2068-2071 |
| Usage | `spread = near_close - far_close` → input to spread_z; entry price for order submission |
| Decision branch | `if near_close <= 0 or far_close <= 0: return None` (validity gate) |
| **Role** | **Spread construction input, execution reference, PnL accounting** |
| **Not role** | Alpha feature — direction is `sign(spread_z)`, not a function of close levels |
| **Verification basis** | `source_file: tmf_spread.py, symbol: on_bar` — validity gate: L2068-2076; spread construction is implicit (spread_z is pre-computed in CSV); entry order uses close as price reference L2189-2190. Spec §4 confirms direction = sign(spread_z) |

### spread_std / spread_mean

| Attribute | Detail |
|---|---|
| Producer | `spread_loader` CSV fields |
| Consumer | `tmf_spread._append_event()` audit log only (L2179) |
| Decision branch | None in `tmf_spread.py` |
| **Role** | **Audit diagnostics** |
| **Not role** | Entry decision |
| **Verification basis** | `source_file: tmf_spread.py` — grep `spread_std` returns only L2179 (audit log write). No `if spread_std` or `expected_profit` in file. Compare `calendar_condor_v2.py` L326-329 where `spread_std` IS a gate |

### expected_profit_points / friction

| Attribute | Detail |
|---|---|
| Status in `tmf_spread.py` | **Absent** — no expected profit calculation, no friction gate |
| Status in `calendar_condor_v2.py` | **Present** — L332-342 computes and gates on expected_profit_points |
| Error risk | Mixing the two strategies produces false dependency |

### Operational / Safety Gates (not derived from futures page parameters)

These exist in MTS entry pipeline but correspond to **no column** in the futures
page parameter list:

| Gate | Code location | Trigger |
|---|---|---|
| Position already open | L2133 | `context.position.size != 0` |
| Re-entry cooldown | L2050-2055 | `_last_exit_ts` within 300s |
| PCF-1 safety gate | L2138-2145 | `channel_safety.get_safety_state()` |
| Session / trading hours | monitor layer | Overnight session rules |
| Duplicate submission | L2148-2150 | `_lifecycle == "SUBMITTING"` |
| Quote age / freshness | `confirm_ms`, `max_quote_age_ms` | Tick-based confirm timer |
| Settlement day | monitor layer | Settlement day position freeze |

**Verification basis:** Each gate located by grepping `tmf_spread.py` for the
specific condition keyword. Negative search: none of these gates consume any
column from the futures page parameter list — they operate on internal state
monotonically.

---

## Corrected False Assumptions

Each entry shows the original claim, the verification result, and the root cause of
misattribution.

| # | Original assumption | Verification result | Misattribution source |
|---|---|---|---|
| 1 | `spread_std` is a formal entry gate in MTS | **False** — only logged in audit (`tmf_spread.py` L2179); IS a gate in `calendar_condor_v2.py` L326-329 | Cross-strategy contamination: the two share `spread_std` as a column name but have different downstream usage |
| 2 | ATR controls position sizing in MTS | **False** — MTS uses fixed 1 contract; `_size_mult()` exists only in `kbar_feature.py` | Cross-strategy contamination: sizing logic from directional strategy assumed to be universal |
| 3 | `expected_profit_points` gates MTS entry | **False** — absent from `tmf_spread.py`; present only in `calendar_condor_v2.py` | Cross-strategy contamination: condor's profit gate assumed to exist in spread engine |
| 4 | MTS entry is "architecturally and permanently minimal by design" | **Unproven** — current code happens to be minimal, but no ADR or spec section declares "entry must always remain spread_z-only" | Reverse inference from implementation to intent; spec §1-4 only describes current behaviour, not architectural constraint |
| 5 | Release purpose ("does directional continuation exist after release?") supports entry minimalism | **Mislayered** — that purpose statement describes post-release single-leg research question (spec §1), not entry design philosophy | Quote taken from §1 (release→single-leg context) and applied to §4 (entry), which is a different decision boundary |
| 6 | ADX > 25 would improve MTS entry | **Hypothesis, unproven** — single-leg ADX may not reflect spread regime; spread-native diagnostics are more appropriate | Directional strategy intuition applied to spread stat-arb without cross-product validation |

### Taxonomy: Strategy Logic Contamination

**Definition:** When analysing strategy A, erroneously attributing logic from
strategy B to A because they share column names, config keys, or dashboard display
but have independent decision paths.

**Why this is dangerous:**

- Column names (`adx`, `spread_std`, `atr`, `score`) are reused across strategies
- Dashboard renders all strategies' data in a unified view, creating visual homology
- Config files may share parameter keys with different semantics per strategy
- "Column exists and is computed" ≠ "column participates in every strategy's decision"
- A plausible-sounding feature (e.g. ADX gate) can pass code reading review if not
  traced to actual decision branch

**Required evidence format for future feature audits:**

```
producer:        <module / data source>
consumer:        <strategy / function>
decision branch: <exact condition that gates or modifies behaviour>
downstream:      <what happens when gate triggers>
strategy owner:  <strategy class / module name>
```

Mere existence of a column or parameter is insufficient to assert participation.

---

## Existing Implementation Inventory

`grep` for spread-native diagnostics in the repo — results are sparse:

| Pattern | Hits | Classification |
|---|---|---|
| `half.?life\|halflife` | 0 | — |
| `mean.?cross\|crossing.frequency` | 0 | — |
| `ar.?1\|autoregress\|variance.?ratio\|hurst` | 0 | — |
| `z.?score.*persist\|persistence\|time_above\|bars_above` | 0 | — |
| `spread.*ema\|ema.*spread\|spread_slope` | 0 | — |
| `spread.*adx\|adx.*spread` | 0 | — |

**Conclusion:** No spread-native regime diagnostics exist anywhere in the repo.
The failure mode ("spread extreme that does not revert") is currently invisible to
MTS entry logic.

---

## Core Analysis Unit: The Spread Episode

This audit reveals a fundamental unit mismatch. MTS entry currently decides on a
**single bar** (`spread_z >= threshold → enter`), but the failure mode — extreme
spread that never reverts — is **an episode, not a point**.

A Spread Episode is defined as:

```
START     first bar where |spread_z| >= entry_z threshold
│
├─ peak excursion     max |z| during episode
├─ persistent regime  bars_above_threshold, time_above_threshold
├─ recovery           reversion_progress, spread EMA slope
│
CROSS     z crosses exit_z threshold
CROSS     z crosses zero (mean)
END       |spread_z| < entry_z consistently for N bars
```

**Why this matters for the research sequence below:**

- Episode-scoped statistics (peak, persistence, recovery speed) are more
  informative than bar-level features for understanding reversion failure.
- Counterfactual Lab naturally extends from Point Replay to Episode Replay:
  "what if entry had been blocked at bar 5 of this episode?"
- Half-life, survival analysis, hazard models can all be computed offline
  from episode records, without online decision dependency.
- This aligns with the existing Trajectory Replay methodology.

**Instrumentation priority:** instrument the episode lifecycle before
computing derived statistics (half-life, AR(1)). Episode records are the raw
data; derived quantities can be re-computed offline.

---

## Feature Lifecycle Standard

Every future feature added to MTS (whether shadow or production) should carry
metadata:

```yaml
feature:
    name: <short identifier>

producer:
    <module that computes this feature>

consumer:
    <strategy or decision function that reads it>

status:
    shadow              # collected, not consumed
    | passive_diagnostic # visible in audit log / dashboard only
    | threshold_modifier # adjusts existing thresholds
    | hard_gate         # blocks or allows entry unconditionally
    | production        # integrated into decision path

decision_dependency:
    true | false

dataset:
    <which dataset or schema captures this>

dashboard:
    optional | required | excluded

counterfactual:
    yes | no

removal_policy:
    <condition under which this feature is removed, e.g.
     "remove if unused for 2 releases" or
     "Phase 4 association fails to reject H0">
```

This ensures every feature has a traceable purpose, producer, consumer, and
exit condition — preventing orphan features from accumulating.

---

## Research Sequence

### Phase 3 — Instrumentation (not research)

**Goal:** Build the data collection layer. No hypotheses, no associations,
no decision changes.

**PR boundary:** `PR: Spread-Native Shadow Diagnostics`

**Core invariant:**

> Shadow diagnostics failure must never alter, delay, or block the existing MTS
> decision path.

**Instrumentation scope (episode-first):**

```python
# Episode identity (scoped to session × product)
episode_id              # session-unique identifier
episode_start_ts        # first bar where |z| >= entry_z
episode_duration_sec    # wall-clock age of episode
episode_bar_count       # 5m bars elapsed in episode

# Peak and persistence (per bar/tick)
z_peak_abs              # max |z| during this episode
z_current_abs           # current |z|
z_extreme_duration_sec  # how long current extreme has persisted
z_extreme_tick_count    # tick count inside episode
z_reversion_progress    # (peak_abs - current_abs) / max(peak_abs - exit_z, ε)
z_crossed_entry_threshold_count  # # of times z re-crossed entry threshold
z_returned_inside_threshold      # has z re-entered [−entry_z, +entry_z]?

# Spread slope (per bar)
spread_ema_fast         # e.g. 5-bar EMA of spread
spread_ema_slow         # e.g. 20-bar EMA of spread
spread_ema_diff         # fast - slow
spread_ema_slope        # diff normalised by spread_std

# Mean-crossing counters (per bar, cumulative)
crossings_per_hour          # rolling
median_time_between_crossings
bars_since_last_mean_cross  # bars since z crossed zero
```

**What is NOT done in Phase 3:**

- No half-life estimation (needs episode data first)
- No AR(1) fitting (window-sensitive, session-boundary fragile)
- No ADX computation
- No regime mapping
- No entry_z modification
- No hard gate addition
- No order path modification

**Snapshot / audit log target:** write episode state to the existing MTS event
ledger (`_append_event`) and optionally to a structured dataset
(e.g. `data/episode_snapshots.jsonl`).

---

### Phase 4 — Observational Research

**Goal:** Associate episode features with outcomes.

**Required dataset:** Minimum 100 episodes accumulated from Phase 3.

**Stratification:**

```
spread_z sign × session × regime × entry ATR bucket
```

**Outcome variables:**

```
MAE (max adverse excursion from entry)
MFE (max favorable excursion from entry)
time_to_mean (bars until z crosses zero)
time_to_exit (bars until z crosses exit_z threshold)
failure_to_revert (binary: |z| never returned inside [−exit_z, +exit_z])
final PnL per episode
```

**Minimum report per episode feature:**

| Feature | Mean | Median | Std | N | Win-rate | PnL/trade |
|---|---|---|---|---|---|---|
| z_persistence_short | ... | ... | ... | ... | ... | ... |
| z_persistence_medium | ... | ... | ... | ... | ... | ... |
| z_persistence_long | ... | ... | ... | ... | ... | ... |
| spread_slope_flat | ... | ... | ... | ... | ... | ... |
| spread_slope_expanding | ... | ... | ... | ... | ... | ... |
| spread_slope_reverting | ... | ... | ... | ... | ... | ... |

**Decision rule for Phase 5 promotion:**

> A feature may proceed to counterfactual evaluation only if it shows a
> non-overlapping confidence interval on at least one outcome variable
> (e.g. failure_to_revert rate differs between high-persistence and
> low-persistence episodes at 95% CI).

**Half-life governance (still shadow-only here):**

Rolling half-life MAY be computed offline for comparison with persistence
metrics, but:

- Must use the episode dataset, not real-time estimation
- Must report φ ≥ 1 episodes separately (non-stationary regime)
- Must NOT gate entry
- Must document window sensitivity (run at 3 window sizes, report all)

---

### Phase 5 — Counterfactual Evaluation

**Goal:** Replay historical episodes with threshold modifications applied.
Do NOT change live strategy yet.

**Methodology:**

For each candidate feature (z-persistence, spread slope, crossing frequency):

1. Select a threshold value (e.g. `block entry if bars_above_threshold > 12`)
2. Apply counterfactual rule to the Phase 3 episode dataset
3. Compare original vs counterfactual outcomes:

```
trades_blocked
missed_profitable_trades
avoided_losing_trades
net_PnL_delta (with bootstrapped CI)
coverage_loss (how many trades would we skip?)
```

**Counterfactual types (in order of increasing aggression):**

```
1. Threshold modifier:  raise entry_z when persistence is high
2. Postpone entry:      wait N bars inside episode before entering
3. Soft gate:           reduce position size when persistence is high
4. Hard gate:           block entry when persistence exceeds bound
```

**Decision rule for Phase 6 promotion:**

> A counterfactual rule must show:
> - Non-negative net PnL delta with 90% CI lower bound > 0
> - Coverage loss < 30% (i.e. at least 70% of original trades retained)
> - No catastrophic failure mode in top-10-worst episodes

---

### Phase 6 — Decision Integration

**Goal:** Integrate the feature into production entry decision.

**Requirements before integration:**

- Passed Phase 5 counterfactual
- Live shadow monitoring for 1 release cycle (confirm no runtime errors)
- Roll-window and session-boundary regression tests pass
- Performance overhead measured (< 1ms per tick on target hardware)
- Feature lifecycle metadata committed to repo alongside production code

**Integration patterns (worst to best):**

```
❌ Hard gate → risky, hard to remove
⚠️ Threshold modifier → better, but still affects all trades
✅ Shadow threshold + alert → system logs "would have blocked" but doesn't
✅ Graduated rollout → feature engages at 25% / 50% / 100% over releases
```

**Recommended pattern:** deploy as threshold modifier with initial cap at 50%
engagement rate, monitored for one release cycle before promotion to 100%.

---

## Full Pipeline Summary

```
R-004
Dependency Audit ─────────────────────────────────────── ✓ current file
│
▼
Phase 3
Instrumentation ───────── episode identity, persistence, slope, crossings
│                        core invariant: no decision impact
▼
Phase 4
Observational Research ── associate features with MAE/MFE/time-to-mean
│                        half-life as offline shadow diagnostic
▼
Phase 5
Counterfactual Evaluation ─ replay episodes with modified thresholds
│                          PnL delta, coverage loss, failure modes
▼
Phase 6
Decision Integration ──── threshold modifier → graduated → production
                         feature lifecycle metadata committed
```

This pipeline is more rigorous than "add a feature that seems useful."
It follows the same Evidence-First methodology already established in the repo.

---

## Phase 3 Acceptance Criteria

These criteria MUST be met before Phase 3 Instrumentation PR is merged.

| # | Criterion | Verification |
|---|---|---|
| 1 | No decision-path modification | `git diff --stat` shows only new files or additions to audit-log paths; zero changes to `if abs(spread_z) < entry_z` or any order submission |
| 2 | No new hard gate | `rg "return None\|block\|gate\|skip"` on new files returns zero hits outside test code |
| 3 | No timing regression > 5% | Benchmark on_bar() with and without instrumentation; report mean/median/p99 latency |
| 4 | Episode reconstructed deterministically | Given same tick/bar sequence, episode_id and all derived fields produce identical output on replay |
| 5 | Shadow diagnostics reproducible | Episode dataset written to append-only log; re-reading produces same records |
| 6 | Existing replay parity unchanged | Run existing backtest suite with and without instrumentation; PnL per trade matches to 4 decimal places |
| 7 | Existing tests unchanged | `pytest` count: 0 new failures, 0 new skips, 0 modified assertions outside trade volume |
| 8 | Additional telemetry only | PR description lists every new log/field/event; no hidden config flags, no conditional branches in production decision path |

**Fail-closed rule:** If any criterion is violated, the PR is rejected. The
instrumentation module may be added as a standalone file with no import in the
production MTS decision path; integration is a separate PR under Phase 4.

---

## Decision

| Dimension | Value |
|---|---|
| **Evidence Level reached** | E3 (verified by code trace & domain review) |
| **Recommended next step** | Phase 3: implement z-score persistence as shadow diagnostic |
| **Impact on strategy** | None until Phase 5-6 completes |
| **Immediate action** | Create this research note; close upstream enquiry with corrected map |

---

## Failure Modes

- **Cross-strategy contamination:** A future analyst may repeat the same error.
  Mitigation: the `Strategy Logic Contamination` taxonomy above serves as a reusable
  warning template.
- **Z-score persistence over long episodes:** A single extreme episode lasting
  multiple days (e.g. yield curve regime shift) would produce high persistence
  without being a trading failure. Must be session-scoped.
- **Spread EMA slope during roll window:** Near-far spread structure changes
  discontinuously during contract roll. Diagnostics should be gated or reset on
  roll detection.
- **ATR bucket granularity:** Too many buckets → overfitting; too few → regime
  mixing. Start with 3 buckets (low / normal / high) matching existing entry_z
  regime.

---

## Strategic Conclusion: Answer to the Original Question

The upstream question was: *"哪些 futures page 參數對 MTS entry 有幫助？"*

**Answer:** None of the ~65 dashboard parameters beyond `spread_z` and `ATR`
directly participate in MTS entry today. More importantly, evidence does not
support adding any of them as hard gates at this time.

The dependency audit revealed that the real bottleneck is not missing entry
features — it is an incomplete evidence chain. The following priority sequence
derives from what is already verifiable in the repo versus what remains unknown.

| Priority | Workstream | Evidence base | Expected impact |
|---|---|---|---|
| ① | Exit / PED analysis | Existing real trades with Profit Extraction Deficit verified via Counterfactual Lab | High |
| ② | Episode Dataset | Repository has Trade, Decision Point, and Trajectory, but no Market Behaviour Unit | High |
| ③ | Failed episode common features | Can be verified once Episode Dataset exists | High |
| ④ | Entry threshold modifier | Counterfactual-ready via Episode persistence | Medium-High |
| ⑤ | Position sizing (capital allocation) | Requires large sample; trade cost ratio is a known unquantified factor | Medium |
| ⑥ | Single-leg directional features (ADX, RSI, Regime, Squeeze) | **No evidence** supports these today | Low |

### Key concern: does MTS still operate under mean reversion?

Recent observations of low ATR with persistent spread expansion (no reversion)
suggest a more fundamental question than "which entry feature to add":

> Which episodes exhibit mean reversion at all?

If the spread regime has shifted, entry accuracy is not the problem — the
strategy's core assumption is. This reinforces the priority of Episode Dataset
over any new entry feature.

### Trade cost ratio and capital allocation

MFX calendar spread trades carry fixed friction (commission, tax, slippage)
that reduces net expectancy on small-excursion trades. Once Episode quality
stratification is mature, two capital allocation questions become addressable:

- Should high-quality episodes use MXF (large contract, higher capital efficiency)?
- Should low-quality episodes be skipped rather than traded at fixed frequency?

This is a capital allocation question, not a signal question — and may affect
final PnL as much as any new feature.

### Recommended posture

> Build the Episode evidence chain before adding any entry feature.
> The answer to "which parameter helps entry" is currently:
> *none proven yet — instrument first, evaluate second, decide third.*

