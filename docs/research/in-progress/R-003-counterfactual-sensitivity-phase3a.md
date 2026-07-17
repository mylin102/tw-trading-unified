# R-003: Counterfactual Release Decision Sensitivity — Phase 3A

**Status:** ACCEPTED
**Start Date:** 2026-07-17
**End Date:** 2026-07-17
**Evidence Level:** E3 (Counterfactual Evaluation)
**References:** ADR-009, ADR-015, ADR-016, R-002

---

## 1. Scope

| Property | Value |
|----------|-------|
| **Cohort** | RELEASE-only decisions (34 cases from R-002) |
| **Tested Parameter** | `release_stop_threshold` |
| **Tested Range** | 6–20 points |
| **Dataset** | Same frozen source as R-002 (`parity-final-v6`) |
| **Cell Count** | 34 cases × 8 levels = 272 |
| **Primary Metric** | Decision flip (RELEASE → NONE) |

### Sensitivity Classification

```
NON_BINDING_WITHIN_TESTED_RANGE
```

Not "insensitive" globally — the tested range did not intersect the empirical decision boundary for any case in this cohort.

### Applicability

- **Applies to:** RELEASE decisions where the release-leg PnL magnitude (111–820 pts) far exceeds the threshold (6–20 pts)
- **Does NOT apply to:** NO_RELEASE cases, near-boundary decisions, threshold ranges above ~50 pts, or confirmation/timing parameters
- **Does NOT establish:** Global threshold insensitivity, PnL outcome sensitivity, or non-binding status for threshold values ≥ 99 pts

---

## 2. Selection Bias

This experiment uses only historical RELEASE cases. The dataset naturally lacks:

| Missing Cohort | Why It Matters |
|----------------|----------------|
| **Threshold not reached → NO_RELEASE** | Would test whether **loosening** the threshold causes earlier RELEASE |
| **Threshold nearly reached → NO_RELEASE / WAIT** | Would test whether **tightening** the threshold blocks marginal releases |
| **Threshold barely crossed → RELEASE** | Boundary cases that would actually flip at small threshold changes |

**Impact:** The 100% decision stability observed applies only to the RELEASE-only cohort. A dataset that includes near-boundary decisions could show threshold sensitivity even within the 6–20 range.

---

## 3. Problem

R-002 established that the production `evaluate_lifecycle_actions()` can deterministically reproduce 34/34 historical RELEASE decisions from reconstructed pre-decision state.

The open question is: **how sensitive are these release decisions to parameter changes?**

Specifically: if the release_stop_threshold were tighter or looser, would the same decisions still be made? This is a prerequisite for any parameter optimization — if the threshold is not a binding constraint, optimizing it is meaningless.

**Hypothesis:** The release_stop_threshold is a binding constraint for at least some historical RELEASE decisions. Tightening the threshold below the historical value should cause at least some decisions to flip from RELEASE to NONE.

---

## 4. Method

### Experiment Design
- **Parameter:** `release_stop_threshold`
- **Levels:** [6, 8, 10, 12, 14, 16, 18, 20]
- **Cases:** 34 RELEASE decisions (same set from R-002)
- **Cells:** 34 × 8 = 272
- **Design:** One-factor-at-a-time (OFAT), full factorial over levels
- **Override:** `release_stop_threshold` injected into `LifecycleContext` before calling `evaluate_lifecycle_actions()`

### Metrics Collected Per Cell

| Metric | Description |
|--------|-------------|
| `decision_changed` | Did the replay action differ from historical? (RELEASE vs NONE) |
| `replayed_action` | What the engine returned with modified threshold |
| `historical_margin` | How far past the historical threshold the PnL was (PnL - threshold) |
| `flip_threshold` | The critical threshold at which decision would flip (if any) |

### Aggregate Metrics Per Level

| Metric | Description |
|--------|-------------|
| `decision_stability` | Fraction of cases where decision did NOT change |
| `decision_change_rate` | Fraction of cases where decision changed |
| `flip_count` | Number of cases with a known flip threshold |

---

## 5. Dataset

Same frozen source as R-002:

| Property | Value |
|----------|-------|
| Generation | `parity-final-v6` |
| Content hash | `f370cc1794f1208b...` |
| Source | `data/frozen/parity_final/` |
| Closed trades | 34 |
| Eligible RELEASE | 34 |

---

## 6. Results

### Per-Level Summary

| Level | Change Rate | Stability | Flips |
|-------|------------|-----------|-------|
| 6 | 0.0% | 100.0% | 0 |
| 8 | 0.0% | 100.0% | 0 |
| 10 | 0.0% | 100.0% | 0 |
| 12 | 0.0% | 100.0% | 0 |
| 14 | 0.0% | 100.0% | 0 |
| 16 | 0.0% | 100.0% | 0 |
| 18 | 0.0% | 100.0% | 0 |
| 20 | 0.0% | 100.0% | 0 |

**Decision stability:** 100% across all 272 cells.

### Margin Analysis

The historical margins (how far PnL was past the threshold) for the 34 cases:

| Metric | Value |
|--------|-------|
| Minimum (closest to boundary) | -213.0 pts |
| Maximum (furthest past boundary) | -1115.7 pts |
| Mean | -385.5 pts |
| Median | -322.4 pts |

The closest case (`mts-auto-183154-722`) had a far-leg PnL of -114 pts against a historical threshold of 99 pts — still 213 pts past the nearest test threshold of 6. No cases were within 200 pts of the boundary.

### Decision Boundary Dataset

No decision flips occurred. The `decision_boundary.parquet` file is empty.

**Two possible interpretations:**

1. **Range didn't intersect boundary** — All 34 cases are well outside the tested threshold range (6–20). The critical threshold for these cases likely lies between 99 (lowest historical threshold) and ~820 (largest PnL magnitude). The tested range simply never reached it.
2. **Boundary doesn't exist** — The decision would never flip regardless of threshold (e.g., if the exit is purely time- or state-driven). This is unlikely for threshold-gated releases.

**Current evidence favors interpretation 1.** The theoretical critical threshold for each case is:
```
critical_threshold ≈ |PnL| + ε  (the PnL magnitude at evaluation time)
```
For the closest case (|PnL| = 213 pts), testing 6–20 pts was never going to flip it. The boundary wasn't "absent" — it was at ~213 pts, well outside the tested range.

---

## 7. Four-Dimensional Analysis

### Decision Stability
- 100% stable across all threshold levels (6–20)
- No parameter value in this range changes any of the 34 release decisions
- `release_threshold` is non-binding within the tested range for this RELEASE-only cohort

### PnL Sensitivity
- Cannot be measured because no decisions changed
- PnL sensitivity requires a different experiment design (e.g., measure PnL outcomes at different thresholds, not just decision flips)

### Boundary Distance
- All 34 cases are well past the threshold boundary
- Minimum distance: 213 pts
- The empirical decision boundary likely lies at or above the smallest historical threshold (~99 pts), far above the tested range

### Case Clustering
- 0 cases flipped at any level within the tested range
- 0 cases have a known boundary within the tested range
- All 34 cases are `NON_BINDING_WITHIN_TESTED_RANGE`

---

## 8. Interpretation

### Why No Flips Occurred — Threshold Saturation

The release_stop_threshold values recorded in this dataset range from 99 to 295 pts. The test range (6–20) is far below even the smallest historical threshold. The PnL values that triggered release range from 108 to 820 pts — far exceeding even threshold=20.

```
|PnL| >>> threshold
```

All cases were already 111 to 820 points beyond any threshold in the tested range at evaluation time. This is **threshold saturation**, not global insensitivity: the threshold gate is not the binding constraint for these cases.

### Implications

1. **release_stop_threshold is NON_BINDING_WITHIN_TESTED_RANGE for this cohort.** It can be varied across 6–20 without changing any release decision.

2. **This does not mean threshold is globally non-binding.** A wider range (0–500) or different dataset (near-boundary NO_RELEASE cases) would likely show sensitivity.

3. **The real binding constraint for this cohort is likely elsewhere:**
   - Confirm ticks / confirm_ms — trajectory-level gating mechanism
   - Stateful guards (warmup, entry age)
   - Regime gating
   - Market regime / volatility regime at decision time

---

## 9. Threats to Validity

### Internal
- **Counterfactual Identifiability / Replay Limitation**: Point Replay is restricted to Decision Counterfactual evaluation. Any timing-dependent stop (such as a tighter stop triggering earlier in the trade's history) cannot be identified using static snapshot replays and requires Trajectory Replay.
- Threshold override injection is narrow (6–20 range). Wider range (1–50 or 20–500) might show different results.
- Only one parameter tested. Interaction effects between threshold and confirmation ticks cannot be detected.
- PnL estimation relies on snapshot prices which may not reflect the exact tick at decision time.
- **Selection bias:** RELEASE-only cohort systematically excludes near-boundary and NO_RELEASE cases.

### External
- 34 cases from one broker/symbol may not generalize.
- Results may differ for different market regimes or contract months.
- Dataset spans only ~8 trading days; longer periods may include boundary cases.

### Construct
- "Sensitivity" is defined as decision flip (RELEASE → NONE). A softer definition (e.g., threshold affecting release timing rather than binary decision) is not captured.
- PnL outcome impact of threshold changes is not measured — only decision change.

---

## 10. Phase 3B Gate Audit: Counterfactual Identifiability

Before proceeding to Phase 3B (confirmation parameter sensitivity), a gate audit was conducted to determine whether the case contracts preserve sufficient trajectory-level evidence for counterfactual analysis.

### Audit Method

Trace the confirmation path for a single RELEASE decision:

```
Threshold crossed → Timer started → Ticks accumulated → Timer satisfied → Release committed
```

Each stage was checked against what `DecisionReplayCase` preserves.

### Audit Results

| Question | Required? | Preserved? |
|---|---|---|
| First crossing timestamp | Yes (timer start) | **No** |
| Tick sequence after crossing | Yes (consecutive count) | **No** |
| Timer reset conditions | Yes (prevent stale timers) | **No** |
| Quote dedup state | Yes (avoid double-counting) | **No** |
| `confirm_tick_count` at decision time | Yes (pre-decision state) | **No** (field exists, always None) |
| `confirm_elapsed_ms` at decision time | Yes (pre-decision state) | **No** (field exists, always None) |

### Mechanism Analysis

In `_manage_position()` (line 2954-2978 of `tmf_spread.py`), the tick confirmation operates on *trajectory-level* state maintained across multiple ticks:

```python
# Tick confirmation state (persistent across ticks)
self._release_near_ticks += 1
if self._release_near_ticks == 1:
    self._release_near_start_time = time.monotonic()

# Gate: backtest bypasses entirely
if not (_is_backtest or (ticks >= confirm_ticks and timer >= confirm_ms)):
    return None  # Blocked — decision deferred
```

Critical detail: `_is_backtest=True` **bypasses the entire confirmation gate**. The replay engine always sets `is_backtest=True`, so confirmation never exercises.

### What Replay Preserves vs What Confirmation Requires

```
REPLAY PRESERVES (Decision Snapshot):
Entry
 │
 │
 ▼
Threshold Cross  ──→  RELEASE        ←──  only this column
 │                                      │
 │                                      │
 ▼                                      ▼
Tick Confirm                      NOT preserved
 │
 │
 ▼
Timer Confirm
 │
 │
 ▼
Release

CONFIRMATION REQUIRES (Decision Trajectory):
Entry → Cross → Tick[1] → Tick[2] → ... → Tick[N] → Timer OK → Release
                        ↑                              ↑
                  tick sequence                 monotonic timer
                  dedup state                   lifecycle events
```

The current `DecisionReplayCase` captures only the final pre-release snapshot (last column). The entire trajectory from crossing to release is not preserved.

### Conclusion

```
Replay Capability          : YES     (core/replay_release.py works)
Experiment Capability     : YES     (core/experiment.py works)
Evidence Sufficiency      : NO      (case contract lacks trajectory data)

Decision counterfactual   : IDENTIFIABLE ✅   (snapshot-level predicate)
Timing counterfactual     : NOT IDENTIFIABLE ❌ (requires trajectory-level evidence)
```

This is **not** a limitation of the replay engine or experiment layer. It is an **evidence model limitation**: the current replay contract preserves decision-state snapshots, not decision trajectories. The distinction is critical:

- **Replay Capability** — the engine can replay any decision given sufficient state
- **Experiment Capability** — the layer can run any counterfactual experiment given an identifiable parameter
- **Evidence Sufficiency** — the current case contract does not contain the evidence required for timing counterfactuals

If trajectory-level evidence were added to `DecisionReplayCase` in the future (crossing timestamp, tick sequence, timer events), the same replay engine could support Phase 3B without architectural changes.

### Gateway Decision

| Component | Identifiable | Evidence Requirement |
|-----------|-------------|---------------------|
| Release threshold | ✅ | Snapshot PnL vs threshold |
| BB position | ✅ | Snapshot `bb_position` field |
| Tick confirmation (count) | ❌ | Tick-level trajectory |
| Confirmation timer (ms) | ❌ | Monotonic clock events |
| Quote/dedup state | ❌ | Runtime timer lifecycle |
| VWAP crossing | ❌ | Bar-level tick context |
| Exit trigger (ATR/VWAP) | ✅ | Bar-level indicators |

---

## 11. Next Steps

### R-003A-1: Expand Threshold Range & Boundary Search

Rather than static sweep bounds (6–20), future studies should implement **Adaptive Boundary Sampling**:
* An adaptive sampling strategy centered around the estimated decision boundary may improve boundary resolution.
* This involves using experiment-specific strategies (such as relative percentage offsets for thresholds, ratios for multipliers, or logarithmic scales for timers) to map the true `decision_boundary.parquet` where transitions (e.g. from `RELEASE` to `NONE`) occur.


### R-003A-2: NO_RELEASE Case Collection & State Coverage

A more impactful next step: collect evaluation snapshots from cases where threshold was **nearly reached but release did not occur**.
* Extend the trade dataset to include all evaluation snapshots (not just the final RELEASE one).
* Establish a state coverage target across regimes, squeeze states, and sessions.

### R-003B: Confirmation Sensitivity — Deferred

**Status:** DEFERRED
**Reason:** Timing counterfactual is NOT IDENTIFIABLE under the current evidence model.

```
Deferred because:

Current replay contract (DecisionReplayCase) preserves
decision-state snapshots rather than decision trajectories.

Additional trajectory-level evidence is required before
timing counterfactuals can be evaluated.

Specifically needed:
  - crossing timestamp (timer start)
  - tick sequence after crossing (consecutive count, dedup state)
  - timer reset events (lifecycle events)
  - quote dedup state (prevent double-counting)
```

This is not "just not started yet." It is a known evidence model limitation: evaluating confirm_ticks / confirm_ms requires transitioning from **Point Replay** (snapshot-level) to **Trajectory Replay** (tick-level sequence + timer lifecycle). The evidence gap must be closed before the experiment can proceed.

**Prerequisite:** A separate research phase to define what tick-level evidence needs to be collected, in what format, and whether `DecisionReplayCase` should be extended or a new `DecisionTrajectoryCase` contract created.

---

## 12. Formal Finding

### Finding A: Threshold Saturation (Phase 3A Result)

```
R-003 Phase 3A Finding

Within the historical RELEASE-only cohort and the tested threshold
range of 6–20 points, release_threshold was non-binding.

All 34 cases were already between 111 and 820 points beyond the
release condition at evaluation time, producing 100% decision
stability and zero observed decision flips.

This result does not establish global threshold insensitivity.
It establishes threshold saturation in the selected RELEASE cohort
and indicates that the tested range did not intersect the empirical
decision boundary.

Sensitivity classification: NON_BINDING_WITHIN_TESTED_RANGE
Architecture status: ACCEPTED
Empirical finding: ACCEPTED WITH RANGE AND COHORT LIMITATION
```

### Finding B: Evidence Model Boundary (Cross-Cutting)

The most important result from R-003 Phase 3A is not the threshold sensitivity conclusion — it is the discovery of the **current evidence model's boundary**.

```
R-003 Phase 3A uncovered:

  Decision Counterfactual   : IDENTIFIABLE ✅
  Timing Counterfactual     : NOT IDENTIFIABLE ❌

This is NOT a limitation of:
  - Replay engine (core/replay_release.py)       → works correctly
  - Experiment layer (core/experiment.py)         → works correctly
  - Threshold counterfactual design              → valid

It IS a limitation of:
  - Existing case contract (DecisionReplayCase)  → snapshot-only
  - Available evidence                           → no trajectory data
  - Evidence model                               → supports decisions, not timing
```

This separation is a methodological contribution. It formalizes the distinction between:

```
Decision ≠ Decision Timing ≠ Evidence Sufficiency
```

### What Phase 3A Actually Proved

1. **Experiment Layer architecture verified:** 272 cells with zero exceptions, deterministic, override injection effective, aggregation correct, empty boundary dataset handled gracefully, raw → summary → report chain complete.

2. **Threshold saturation observed:** All cases at |PnL| ≥ 213 pts, tested range only 6–20 pts. This is threshold saturation, not low sensitivity.

3. **Selection bias identified:** RELEASE-only cohort cannot detect sensitivity for near-boundary cases. This is a dataset limitation, not an experiment limitation.

4. **Evidence model boundary discovered:** The existing `DecisionReplayCase` contract is a **Decision Snapshot** contract, not a **Decision Trajectory** contract. This is a qualitative finding that defines the scope of what the current framework can and cannot answer.

### Naming Clarification

The current `DecisionReplayCase` would be more precisely named:

```
DecisionReplayCase (current)
  →  DecisionSnapshotCase (more precise)
```

Because it captures a single pre-decision snapshot — not the tick-by-tick trajectory leading to that decision.

A future trajectory-level contract would be:

```
DecisionTrajectoryCase (future)
```

containing:
  - crossing timestamps
  - tick sequences with dedup state
  - timer lifecycle events (start, reset, satisfy)

Both contract types would coexist. `DecisionSnapshotCase` serves decision-level counterfactuals (threshold, BB, regime). `DecisionTrajectoryCase` would serve timing-level counterfactuals (confirm_ticks, confirm_ms). They would share the same replay engine interface but carry different evidence payloads.

---

## 13. Output

| File | Description |
|------|-------------|
| `reports/research/R-003/experiment_results.parquet` | 272 raw cells |
| `reports/research/R-003/per_level_summary.parquet` | 8 level aggregates |
| `reports/research/R-003/experiment_report.json` | Structured report |
| `reports/research/R-003/sensitivity_summary.txt` | Human-readable summary |

---

## Appendix: Experiment Layer Architecture

```
core/experiment.py
  ExperimentConfig       — describes one experiment dimension
  ExperimentResult       — one cell (case × level)
  generate_experiments() — creates (case, override) pairs
  run_cell()             — single experiment cell
  run_experiment()       — batch run all cells
  analyze_experiment()   — per-level + 4-dim analysis
  save_experiment_results() — parquet + JSON + text output
```

---

## Appendix: Counterfactual Identifiability Framework

Recommended fixed section for all future sensitivity research:

| Component | Identifiable | Evidence Source |
|-----------|-------------|-----------------|
| Release threshold | ✅ | Snapshot PnL vs threshold |
| BB position | ✅ | Snapshot `bb_position` field |
| Tick confirmation | ❌ | Trajectory state not preserved |
| Confirmation timer | ❌ | Monotonic clock not captured |
| VWAP crossing | ❌ | Requires bar-level context |
| Regime gating | ✅ | Captured via bar features |
| Exit trigger (ATR/VWAP) | ✅ | Bar-level indicators available |
