# MTS Entry Qualification Improvement Plan

**Status:** APPROVED  
**Owner:** MTS Strategy  
**Created:** 2026-07-22  
**Target:** Reduce systematic false-positive entries while preserving mean-reversion opportunities

---

## Problem Statement

### Evidence (2026-07-22 TMF Trades)

- 16 consecutive RELEASE stop-loss trades
- 12 losses / 4 wins (25% win rate)
- Net PnL: -540 points
- Every trade exited via RELEASE (none reached TRAIL)
- Same directional entry repeated during persistent spread expansion (~09:00-11:30)

### Root Cause

Current entry logic:

```
Z-score threshold → Immediate entry
```

Assumption: "Extreme Z-score ⇒ Mean reversion is imminent."

Observed reality: Extreme Z-score occurred during **ongoing expansion** — the strategy entered multiple times into a trending/expanding spread without any qualification beyond Z-score.

---

## Guiding Principles

**Do not:**
- Tune entry_z threshold
- Widen RELEASE stop
- Change TRAIL parameters
- Introduce complex ML classifiers

**Do:**
- Preserve deterministic architecture
- Collect evidence before changing production logic
- Implement changes incrementally
- Make every new filter measurable

---

## Research Phase (No Production Changes)

### R-004 — Entry Episode Analysis

**Goal:** Determine why entries fail by introducing the concept of an "Entry Episode."

| Item | Description |
|------|-------------|
| Current | Entry → Release → Entry → Release → (independent trades) |
| Proposed | Episode: Z crosses threshold → expansion continues → candidate entries → eventually returns → episode ends |
| Metrics | episode_id, duration, max_z, max_spread, expansion_distance, candidate_entries, actual_entries, releases_per_episode |
| Deliverable | `reports/research/R004_episode_analysis/` |

**Status:** NOT STARTED

---

### R-005 — Delay Sensitivity Study

**Goal:** Replay every candidate entry with delayed entry (15s, 30s, 45s, 60s).

| Metric | Description |
|--------|-------------|
| Win rate | Did waiting improve win rate? |
| Expectancy | Expected value per trade |
| MAE / MFE | Maximum adverse/favorable excursion |
| RELEASE rate | Percentage of trades stopped out |
| Trade count | How many entries survived the delay |

**Question:** Does waiting actually improve entries?

**Status:** NOT STARTED

---

### R-006 — Expansion Velocity Study

**Goal:** Compare expansion velocity between winning and losing entries.

| Metric | Description |
|--------|-------------|
| dz/dt | Z-score change rate |
| dSpread/dt | Spread change rate |

**Output:** Distribution comparison (winning vs losing entries).

**Status:** NOT STARTED

---

## Shadow Filters (Production-Adjacent, No Effect)

### Filter A — Expansion Velocity

Reject entry when dz/dt exceeds threshold.

```
Candidate entry → Check dz/dt → If > threshold → Would reject (log only)
```

**Status:** NOT STARTED

### Filter B — Momentum Exhaustion

Accept entry only when expansion rate begins slowing (plateau detection).

Example pattern:
```
210 → 213 → 215 → 216 → 216 → 216  (slowing → accept)
210 → 214 → 218 → 223              (accelerating → reject)
```

**Status:** NOT STARTED

### Filter C — Episode Lockout

After a RELEASE in one direction (e.g., SELL_SPREAD), block same-direction entries until:
- Cooldown period expires
- Z-score resets to neutral
- Direction reversal signal fires

**Status:** NOT STARTED

**Deliverable:** `entry_shadow_decisions.parquet`

---

## Counterfactual Evaluation (Phase 3)

Replay historical dataset comparing:

| Variant | Description |
|---------|-------------|
| Baseline | Current logic |
| A | Expansion Velocity only |
| B | Momentum Exhaustion only |
| C | Episode Lockout only |
| A+B | Velocity + Exhaustion |
| A+B+C | All filters |

**Metrics:** Trade count, expectancy, PnL, RELEASE frequency, avg holding time, missed winners

**Acceptance criteria:**
- Trades decrease < 20%
- Expectancy improves
- RELEASE frequency decreases
- PnL improves

**Status:** NOT STARTED

---

## Production Rollout (Phase 4)

**Deploy order:** Episode Lockout → Expansion Velocity → Momentum Exhaustion

Each deployment includes:
- Feature flag
- Shadow logging retained
- Rollback capability

**Status:** NOT STARTED

---

## Architecture Changes

### New Components

| Component | Description |
|-----------|-------------|
| `EntryEpisodeTracker` | Tracks ongoing expansion episodes |
| `EpisodeState` | Current episode metadata |
| `ExpansionVelocity` | dz/dt and dSpread/dt calculator |
| `EntryQualificationResult` | Structured entry decision |
| `ShadowEntryFilter` | Evaluation-only filter runner |

### Unchanged Components

- RELEASE FSM
- TRAIL FSM
- Order execution
- Risk engine
- Position lifecycle
- Broker interface

---

## Validation Gates

A filter may be promoted only if it satisfies:

### Statistical
- Improved expectancy
- Lower RELEASE frequency
- No catastrophic reduction in trades

### Engineering
- Deterministic replay
- 100% reproducible
- No wall-clock dependency

### Operational
- Shadow-tested first
- Feature-flagged
- Rollback supported

---

## Success Criteria

The objective is NOT to maximize win rate.

The objective is to **reduce systematic false-positive entries while preserving valid mean-reversion opportunities.**

Target outcomes:
- Fewer repeated entries within the same expansion episode
- Lower RELEASE stop frequency
- Equal or better expectancy
- Minimal reduction in high-quality trading opportunities
- No changes to existing RELEASE/TRAIL architecture

---

## Milestone Tracking

### Research Phase (Evidence-First, No Decisions)

All R-items establish datasets and answer empirical questions. No filter design until Decision Review.

| ID | Item | Goal | Status | Target Date | Owner |
|----|------|------|--------|-------------|-------|
| R-004 | Entry Episode Analysis | Build Episode Dataset — how many independent opportunities produced 16 trades? | **IN PROGRESS** | — | — |
| R-005 | Delay Sensitivity Study | Does delaying entry (15-60s) improve outcomes? | NOT STARTED | — | — |
| R-006 | Expansion Velocity Study | Do losing entries exhibit higher dz/dt? | NOT STARTED | — | — |
| R-007 | Regime Overlay | Is episode frequency/severity regime-dependent? | NOT STARTED | — | — |

### Decision Gate

```
Research → Evidence Review → Select ONE production change
```

After all R-items are complete, a review selects the single most impactful filter. No deployment before Decision Gate.

### Shadow Filters (Post-Review)

| ID | Item | Prerequisite | Status |
|----|------|-------------|--------|
| C | Episode Lockout (episode-aware, not timer-based) | R-004 confirms multi-entry episodes exist | NOT STARTED |
| A | Expansion Velocity Filter | R-006 confirms velocity discriminates | NOT STARTED |
| B | Momentum Exhaustion Filter | R-005/R-006 support | NOT STARTED |

### Counterfactual Evaluation (Phase 3)

Replay historical dataset comparing selected filter vs baseline.

### Production Rollout (Phase 4)

Feature-flagged, shadow logging retained, rollback supported.
