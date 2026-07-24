# R-002: Release Decision Reproducibility — Final Report

**Status:** Accepted
**Start Date:** 2026-07-16
**End Date:** 2026-07-16
**Evidence Level:** E2 (Deterministic Replay — Counterfactual readiness)
**References:** ADR-009, ADR-010, ADR-015, ADR-016

---

## 1. Problem Statement

The MTS Calendar Spread strategy makes discrete lifecycle decisions (ENTRY, RELEASE, EXIT) based on market state, position PnL, and configured thresholds. Before R-002, there was no ability to:

- Verify that a historical decision would be reproduced given the same inputs.
- Isolate decision logic from execution side effects (orders, state files, timers).
- Measure the determinism of the decision engine.
- Establish a baseline against which counterfactual parameter changes could be measured.

**Hypothesis:** The MTS release decision engine (`evaluate_lifecycle_actions`) is a pure function of (LifecycleContext × PositionLifecycle), and given reconstructed pre-decision state, it will reproduce 100% of historical RELEASE_NEAR/RELEASE_FAR actions.

---

## 2. Dataset Construction

### Source
- Generation: `parity-final-v6`
- Content hash: `f370cc1794f1208b...`
- Source: Frozen copy of `mts_trade_fills.jsonl` + `mts_spread_events.jsonl`
- Frozen at: 2026-07-16T22:50 UTC
- Closed trades: 34, Open: 1

### Decision Records (93 total)
| Type | Count | Scope (Phase 2A) |
|------|-------|-------------------|
| ENTRY | 26 | OUT_OF_SCOPE_ENTRY |
| RELEASE_NEAR | 18 | IN_SCOPE_RELEASE |
| RELEASE_FAR | 16 | IN_SCOPE_RELEASE |
| EXIT_NEAR | 15 | OUT_OF_SCOPE_FINAL_EXIT |
| EXIT_FAR | 18 | OUT_OF_SCOPE_FINAL_EXIT |

### Snapshot Coverage
- Total snapshots: 160 (93 decision-point + 67 observations)
- Decision-snapshot merge: 93/93 (100%)
- Orphan decision-point snapshots: 0
- Missing lifecycle state: 34/34 (documented assumption)

---

## 3. Eligibility Rules

### Scope Classification
- Phase 2A scope: `RELEASE_NEAR` + `RELEASE_FAR` only (34 decisions)
- ENTRY and EXIT decisions: explicitly classified as `OUT_OF_SCOPE`, never counted as replay failures
- Denominator for action-match: 34 release decisions

### Action-Specific Required Fields

**RELEASE (34 cases)**
| Field | Source | Required |
|-------|--------|----------|
| `atr` | Snapshot | Yes (for ATR dynamic mode) |
| `release_stop_mode` | `params_json.risk_mode` | Yes |
| `release_stop_threshold` | `params_json.release_stop` | Yes |
| `direction` | Facts merge | Yes (for PnL direction) |
| `near_side` / `far_side` | Derived from direction | Yes |
| `near_pnl` (entry price) | Facts | Yes |
| `far_pnl` (entry price) | Facts | Yes |
| `near_price` / `far_price` | Snapshot | Yes (at least for released leg) |

### Eligibility Result
| Status | Count | Reason |
|--------|-------|--------|
| ELIGIBLE | 34 | All required fields present |
| UNSUPPORTED_ACTION | 59 | Deferred to other phases |
| Eligibility rate | 100% | (34/34 in-scope) |

---

## 4. Replay Assumptions

### Lifecycle State Reconstruction
Since the current dataset does not capture pre-decision lifecycle state, the replay reconstructs it:

| Field | Reconstructed Value | Justification |
|-------|-------------------|---------------|
| `phase` | `SPREAD` | Release decisions only occur during SPREAD phase |
| `release_group.status` | `ARMED` | Release is triggered from ARMED state |
| `trail_group.status` | `INACTIVE` | Not relevant before release |
| `near_leg_status` | Held | All release cases have prior ENTRY fills |
| `far_leg_status` | Held | All release cases have prior ENTRY fills |
| `remaining_leg` | None | Not yet determined before release |

**Validation:** All 34 release cases occur after an ENTRY decision (decision_seq > 0) and before any EXIT decision (decision_seq < max per trade).

### PnL Direction
PnL is computed using the trade's direction from `trade_facts`:

| Direction | Near Side | Far Side |
|-----------|-----------|----------|
| `SELL_NEAR_BUY_FAR` | SHORT | LONG |
| `BUY_NEAR_SELL_FAR` | LONG | SHORT |

**Unreleased leg:** If a snapshot only records one leg's price (the released leg), the other leg's PnL contribution defaults to 0 (no price movement information available).

### Threshold Derivation
The effective `release_stop_threshold` is extracted from `params_json.release_stop` as recorded at decision time — never from current YAML defaults or runtime configuration.

---

## 5. Deterministic Reconstruction

### Replay Architecture
```
DecisionReplayCase (frozen)
    ↓
LifecycleContext (immutable)
    + PositionLifecycle (fresh)
    ↓
evaluate_lifecycle_actions() (production code)
    ↓
ReplayResult (frozen)
```

### Isolation Guarantees
- Fresh `PositionLifecycle` per case (no state carry-over)
- `copy.deepcopy` of all inputs before engine call
- No `datetime.now()`, `time.time()`, or wall-clock dependencies
- No Shioaji API, no order submission, no state file writes
- No PM2 callbacks, no tick stream, no lifecycle singleton mutation

### Reproduction Results (34 RELEASE cases)

| Metric | Result | Target |
|--------|--------|--------|
| Action match | 34/34 (100%) | 100% |
| Leg match | 34/34 (100%) | 100% |
| Reason match | 34/34 (100%) | 100% |
| Mismatches | 0 | 0 |
| Exceptions | 0 | 0 |

### Determinism Verification

| Test | Result | Method |
|------|--------|--------|
| Forward vs Reverse | Identical | Same 34 cases in opposite order |
| Single-case vs Batch | Same | One-by-one replay vs all-at-once |
| Idempotency | Repeatable | Content hash stable across runs |
| Side effects | NONE | State file + fills log unchanged |

**Reproduction content hash:** `24a11d30ca1f0537...`

---

## 6. Validation Metrics

### Hard Gates (all passed)
- [x] Eligible action match rate = 100%
- [x] Eligible release-leg match rate = 100%
- [x] Eligible target-phase match rate = 100%
- [x] Duplicate replay cases = 0
- [x] Replay side effects = 0

### Coverage Gates (all passed)
- [x] All 93 decisions classified
- [x] All 4 snapshot gaps explained (zero — design fix)
- [x] Eligible decisions replayed = 100%
- [x] Unreplayable cases individually listed (59 deferred, properly classified)

### Reason Gate
- [x] Reason exact/mapped match = 100%
- [x] No vocabulary migration needed — recorded reasons already use current enum

---

## 7. Threats to Validity

### Internal Validity

| Threat | Mitigation |
|--------|------------|
| Lifecycle state reconstruction may differ from actual | Assumptions documented per case; only plausible transitions used |
| NaN snapshots for unreleased leg | PnL defaults to 0; may mask cases where both legs should trigger |
| `params_json` may not capture all effective parameters | Current threshold + risk_mode are sufficient for release check |
| Timestamp-based event correlation has 5-second window | Acceptable given event sequencing in production logs |

### External Validity

| Threat | Mitigation |
|--------|------------|
| Results only cover RELEASE decisions, not ENTRY/EXIT | Scope explicitly limited to Phase 2A |
| 34 cases from one broker (Shioaji) | Dataset spans multiple days/sessions |
| All trades are TMF calendar spreads | Results specific to MTS; generalizability depends on shared engine |
| Reconstruction assumes SPREAD/ARMED state | Validated against decision sequence invariants |

### Construct Validity

| Threat | Mitigation |
|--------|------------|
| "Action match" defined as evaluator returning RELEASE | Matches production semantic; does not verify tick confirmation or BB filtering |
| Release decision may be influenced by confirmation counters | Confirmation state not captured; assumed elapsed in current dataset |
| Manual overrides or operator interventions not tracked | None identified in dataset (no MANUAL decisions in 93 cases) |

### Statistical Validity
- Population: All 34 available RELEASE decisions (not a sample)
- No confidence intervals needed — this is a deterministic reproduction, not a statistical inference
- Results are exact: 100% match on all metrics

---

## 8. Limitations

### Current Dataset Does Not Capture
| Missing Field | Impact |
|---------------|--------|
| `lifecycle_phase_before` | Cannot verify state transition correctness |
| `release_group.status_before` | Cannot verify ARMED→TRIGGERED timing |
| Tick confirmation state | Cannot verify confirm_ticks filter |
| BB filter state at release | R-001 shadow data not yet integrated |
| `entry_age_secs` | Not used by release check, but used by TIMEOUT |
| `max_loss_pts` | Not used by release check, but used by STOPLOSS |

### Replay Scope Limitations
- Only reproduces **positive decisions** (actions that happened)
- Does NOT verify **negative decisions** (timeline non-trigger — whether engine would have released earlier)
- Does NOT verify **cross-trade state isolation** (never tested in production)
- Does NOT cover **ENTRY decision engine** (different evaluator path)
- Does NOT cover **remaining-leg EXIT** (trail engine, profit lock)

### Dataset Limitations
- 34 cases is sufficient for deterministic reproduction but may not cover all edge cases
- Events without trade_id are correlated by timestamp proximity (5s window)
- Some EXIT_LOG events may correlate to wrong trade in edge cases

---

## 9. Future Work

### Immediate
- **R-003: Counterfactual Release Replay** — Change threshold, ATR multiplier, or BB filter and measure outcome impact
- **Entry decision replay** — Apply same infrastructure to 26 ENTRY decisions
- **Remaining-leg EXIT replay** — Trail engine verification (Phase 2B)

### Medium-term
- **Timeline non-trigger verification** — For each trade, replay ALL snapshots (not just decision points) to verify engine does NOT release before recorded decision
- **Lifecycle state capture** — Add lifecycle state to decision log (future schema upgrade)
- **Tick confirmation simulation** — Integrate confirm_ticks data from production log

### Long-term
- **Full lifecycle reproduction** — Chain ENTRY → RELEASE → EXIT replay in sequence
- **Parameter sweep optimization** — Use counterfactual replay as optimization objective
- **CI integration** — Run replay as regression gate after code changes

---

## Appendix A: Replay Infrastructure Diagram

```
trade_dataset.parquet
    │
    ▼
decision_level_view()
    │
    ▼
DecisionReplayCase[]   ← stable, immutable, hashable
    │
    ├── eligibility_classify()
    │   └── 93 cases → 34 ELIGIBLE + 59 DEFERRED
    │
    └── replay_batch()
        │
        ├── reconstruct_lifecycle()
        │   └── PositionLifecycle(SPREAD, ARMED)
        │
        ├── build_release_context()
        │   └── LifecycleContext(near_pnl, far_pnl, threshold)
        │
        ├── evaluate_lifecycle_actions()
        │   └── LifecycleDecision
        │
        └── compare()
            └── ReplayResult { action, leg, reason, hash }
```

## Appendix B: File Inventory

| File | Lines | Purpose |
|------|-------|---------|
| `core/replay_contracts.py` | ~490 | `DecisionReplayCase`, eligibility, reason mapping |
| `core/replay_release.py` | ~300 | `replay_single_release()`, batch, order-independence |
| `scripts/research/phase2a1_eligibility.py` | ~160 | Case builder + eligibility runner |
| `scripts/research/phase2a2_replay.py` | ~220 | Side-effect-free release replay runner |
| `reports/research/R-002/*` | — | Results, metadata, reports |

## Appendix C: Metadata

```json
{
  "research_id": "R-002",
  "status": "Accepted",
  "dataset_generation": "parity-final-v6",
  "dataset_content_hash": "f370cc1794f1208b...",
  "reproduction_content_hash": "24a11d30ca1f0537...",
  "phase_2a_0": "ACCEPTED",
  "phase_2a_1": "ACCEPTED",
  "phase_2a_2": "ACCEPTED",
  "total_decisions": 93,
  "in_scope_release": 34,
  "eligible_release": 34,
  "action_match_rate": 100.0,
  "leg_match_rate": 100.0,
  "reason_match_rate": 100.0,
  "side_effects": "NONE",
  "order_independent": true
}
```
