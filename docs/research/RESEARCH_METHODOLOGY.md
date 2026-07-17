# MTS Research Methodology v1.0

> A framework for evidence-driven counterfactual research in event-driven trading systems.

---

## 1. Why This Document Exists

This is not an ADR (architecture decision record) and not a research report. It is a **standalone methodology reference** that defines the core concepts, taxonomies, and invariants used by the MTS research program.

All research projects (R-NNN) cite this document rather than redefining these concepts. This ensures methodological consistency and allows the framework itself to evolve independently.

---

## 2. Core Distinction: Decision ≠ Timing ≠ Evidence

The single most important insight from the R-003 program:

```
Decision ≠ Decision Timing ≠ Evidence Sufficiency
```

| Concept | Definition | Example |
|---------|-----------|---------|
| **Decision** | Which action was taken | RELEASE vs NONE |
| **Decision Timing** | When the decision occurred (time, tick count, FSM state) | RELEASE at tick 7 vs tick 3 |
| **Evidence Sufficiency** | Whether available data can support a given counterfactual question | Threshold: yes. Confirm ticks: no. |

These three dimensions are independent. A replay engine may correctly replay decisions but lack the evidence to replay timing. This is not a bug — it is an **evidence model boundary**.

---

## 3. Research Pipeline

```text
Research Question
        │
        ▼
Evidence Model         ←  Does the available evidence support this question?
        │
        ▼
Replay Capability      ←  Can the replay engine exercise the relevant code path?
        │
        ▼
Counterfactual Capability ←  Can parameters be varied while keeping other state fixed?
        │
        ▼
Finding
        │
        ▼
Evidence Boundary      ←  What cannot be answered with current evidence?
        │
        ▼
Next Evidence Required ←  What needs to be collected for the next question?
```

---

## 4. Replay Taxonomy

Two replay modes, differentiated by evidence requirement:

### 4.1 Point Replay (Decision Counterfactual)

Replays a single pre-decision snapshot against the production decision engine.

- **Evidence:** DecisionSnapshotCase (pre-decision state: prices, PnL, params, indicators)
- **Questions answered:** Would the decision change if parameter X were different?
- **Limitation:** Cannot model timing, ticks, or FSM state transitions
- **Code:** `core/replay_release.py`, `core/experiment.py`
- **Status:** ✅ Verified (R-002, R-003A)

### 4.2 Trajectory Replay (Timing Counterfactual) — Future

Replays a sequence of ticks/prices leading up to the decision, including FSM state transitions.

- **Evidence:** DecisionTrajectoryCase (tick sequence, crossing timestamps, timer lifecycle)
- **Questions answered:** Would the decision time shift if confirmation ticks were different?
- **Requirement:** Trajectory-level evidence not yet available
- **Code:** Not yet implemented
- **Status:** ❌ Not identifiable under current evidence model

```text
                 ┌─────────────────────────────────────┐
                 │         REPLAY TAXONOMY              │
                 ├──────────────────┬──────────────────┤
                 │  Point Replay    │ Trajectory Replay │
                 │  (Decision)      │ (Timing)          │
                 ├──────────────────┼──────────────────┤
│ Evidence type   │ Snapshot         │ Sequence + FSM    │
│ Contract        │ DecisionSnapshot │ DecisionTrajectory │
│ Questions       │ Parameter change │ Time shift + PnL  │
│ Status          │ VERIFIED         │ NOT IDENTIFIABLE  │
└──────────────────┴──────────────────┴──────────────────┘
```

---

## 5. Counterfactual Identifiability

Not all parameters can be meaningfully varied in counterfactual analysis. Identifiability depends on the evidence model:

```text
IDENTIFIABLE ✅
  Parameter is a snapshot-level predicate.
  Evidence: captured in a single pre-decision state.

NOT IDENTIFIABLE ❌
  Parameter operates on trajectory-level state.
  Evidence: requires tick sequence, timer events, FSM history.
```

### Identifiability Matrix

| Parameter | Identifiable | Required Evidence |
|-----------|-------------|-------------------|
| Release threshold | ✅ | Snapshot PnL vs threshold |
| BB position | ✅ | Snapshot `bb_position` field |
| Regime gating | ✅ | Bar-level regime indicator |
| ATR multiplier | ✅ | Snapshot ATR value |
| Confirm ticks count | ❌ | Tick-level trajectory |
| Confirm timer (ms) | ❌ | Monotonic clock events |
| Quote age / dedup | ❌ | Runtime timer lifecycle |
| VWAP crossing | ❌ | Bar-level tick context |

---

## 6. Evidence Model

### 6.1 Definition

> The evidence model defines what data the system preserves about a decision, and therefore what counterfactual questions it can answer.

A decision is not just a row in a log. It is an **event produced by a process that consumes state over time**. The evidence model determines which parts of that process are recoverable.

### 6.2 DecisionSnapshotCase (Point Replay)

Current contract. Captures a single pre-decision snapshot:

```python
@dataclass(frozen=True)
class DecisionSnapshotCase:
    # Identity
    trade_id: str
    decision_seq: int
    decision_timestamp: str

    # Recorded result
    recorded_action: str        # RELEASE, ENTRY, EXIT, etc.
    recorded_reason: str | None
    recorded_params_json: str | None

    # Pre-decision state
    near_price: float | None
    far_price: float | None
    spread: float | None
    z_score: float | None
    atr: float | None
    near_pnl: float | None
    far_pnl: float | None

    # Configuration
    release_stop_threshold: float | None
    confirm_ticks_required: int | None  # always None — not populated
    confirm_ms_required: int | None     # always None — not populated
```

**Strengths:**
- Deterministic and immutable
- Zero side effects on replay
- Supports decision-level counterfactuals

**Limitations:**
- No tick sequence before decision
- No crossing timestamp
- No timer lifecycle
- Clock state and FSM state not preserved

### 6.3 DecisionTrajectoryCase (Trajectory Replay) — Future

Proposed contract. Captures the tick-by-tick path to a decision:

```python
@dataclass(frozen=True)
class DecisionTrajectoryCase:
    # Identity (same as snapshot case)
    trade_id: str
    decision_seq: int
    decision_timestamp: str

    # Trajectory evidence
    crossing_timestamp: str       # when threshold was first breached
    crossing_price: float         # price at first crossing

    tick_sequence: list[dict]    # ordered list of ticks between cross and release
    # each tick: {ts, near_price, far_price, near_pnl, far_pnl, quote_valid}

    timer_started_at: float | None   # monotonic timer at first crossing
    timer_reset_events: list[dict]   # any timer resets before decision
    confirm_tick_count: int          # actual accumulated ticks before decision
    confirm_elapsed_ms: int          # actual elapsed ms before decision

    dedup_state: dict | None         # last-seen quote hashes to prevent double-count

    # Snapshot (shared with DecisionSnapshotCase)
    recorded_action: str
    recorded_reason: str | None
    recorded_params_json: str | None
```

**Requirements for collection:**
- Production tick logging at crossing events
- Timer lifecycle event capture (start → accumulate → satisfy / reset)
- Quote dedup state serialization
- FSM state snapshots at each tick boundary

---

## 7. Evidence Sufficiency

> Given a research question, does the available evidence support a valid inference?

### Assessment Protocol

For each candidate parameter in a counterfactual study:

1. **Is the parameter a snapshot-level predicate?**
   - ✅ → Identifiable. Proceed with Point Replay.
   - ❌ → Falls in trajectory-level category. Check evidence.

2. **Does the case contract contain the required trajectory evidence?**
   - ✅ → Potentially identifiable. Requires Trajectory Replay.
   - ❌ → Not identifiable. Document as Evidence Boundary.

3. **Is the evidence sufficient for the intended inference?**
   - Consider: sample size, state coverage, timing fidelity, data quality

### Sufficiency Classification

| Label | Meaning | Action |
|-------|---------|--------|
| SUFFICIENT | Evidence supports valid inference | Proceed |
| BOUNDARY | Evidence supports partial inference | Document limitation |
| INSUFFICIENT | Evidence cannot support any inference | Defer, collect more |

---

## 8. Evidence Boundary

### 8.1 Definition

> The boundary beyond which the available evidence can no longer support valid inference for a given research question.

This is not a limitation of the replay engine or experiment tooling. It is a **measured property of the evidence model** at a given point in time.

### 8.2 Examples

**R-003 Phase 3A:**

```text
Evidence Boundary
─────────────────────────────────────────────────
Can Answer:     Threshold sensitivity (6–20 pts)
                Decision stability (RELEASE-only cohort)

Cannot Answer:  Confirmation tick sensitivity
                Confirmation timer sensitivity
                Timing-dependent parameter changes

Boundary Cause: Case contract is DecisionSnapshotCase.
                Trajectory-level evidence not preserved.
```

**R-001 (anticipated):**

```text
Evidence Boundary
─────────────────────────────────────────────────
Can Answer:     BB position distribution at release

Cannot Answer:  BB position trajectory over time
                BB position vs regime interaction

Boundary Cause: Insufficient state coverage across regimes/sessions.
```

### 8.3 Every Research Report Should Have One

```text
Evidence Boundary

Current Evidence
    ↓
Can Answer:
    - ...
    - ...

Cannot Answer:
    - ...
    - ...

Next Evidence Required:
    - ...
    - ...
```

---

## 9. State Coverage

Counterfactual validity depends on whether the available cases cover the relevant state space.

### Coverage Dimensions

| Dimension | Description | Current Status |
|-----------|-------------|----------------|
| **Decision type** | Are all action types represented? | RELEASE only. ENTRY and EXIT deferred. |
| **PnL depth** | Range of loss magnitudes at decision | 108–820 pts (tilted large) |
| **Regime** | Market regimes (trend, range, volatile) | Not yet analyzed |
| **Session** | Day vs night trading sessions | Not yet analyzed |
| **Contract month** | Near vs far month behavior | Not yet analyzed |
| **Time period** | Trading days covered | ~8 days |

### Coverage Targets

A coverage target defines the minimum state diversity required for a valid counterfactual study:

```text
For threshold sensitivity:
  - At least 10 cases with |PnL| within 1× threshold range
  - At least 5 cases from each regime type
  - At least 3 cases per session (day/night)
```

If these targets are not met, selection bias must be explicitly documented.

---

## 10. Research Output Standard

Every R-NNN report must include:

### Required Sections

```text
1.   Scope               — cohort, parameters, range, dataset
2.   Selection Bias      — what cases are missing and why it matters
3.   Evidence Model      — what evidence was available
4.   Counterfactual Identifiability — which params are identifiable
5.   Results             — raw + summary + analysis
6.   Threats to Validity — internal, external, construct
7.   Evidence Boundary   — what can and cannot be answered
8.   Next Evidence Required — what to collect next
```

### Recommended Appendix

```text
Counterfactual Identifiability Framework

Component             Identifiable   Evidence Source
─────────────────────────────────────────────────────
Release threshold     ✅             Snapshot PnL vs threshold
BB position           ✅             Snapshot bb_position
Tick confirmation     ❌             Trajectory state missing
Confirmation timer    ❌             Monotonic clock not captured
Regime gating         ✅             Bar features available
```

---

## 11. Relationship to Other Documents

| Document | Relationship |
|----------|-------------|
| **ADR-009** | Defines lifecycle FSM (SPREAD → ARMED → ... → SINGLE_LEG). Methodology uses this as source of truth for replayable states. |
| **ADR-015** | Defines release theory (thresholds, confirmation). Methodology's identifiability analysis depends on knowing which parameters are snapshot vs trajectory. |
| **ADR-016** | Defines evidence levels (E0–E7). Methodology's evidence sufficiency and boundary analysis operates within this lifecycle. |
| **R-002** | First demonstration of Point Replay. Established DecisionSnapshotCase contract and reproduction methodology. |
| **R-003** | Applied Point Replay to counterfactual analysis. Discovered evidence model boundary (Decision ≠ Timing). Formalized DecisionTrajectoryCase concept. |

---

## 12. Changelog

| Version | Date | Changes |
|---------|------|---------|
| v1.0 | 2026-07-17 | Initial methodology. Defines Replay Taxonomy, Evidence Sufficiency, Evidence Boundary, Counterfactual Identifiability. |
