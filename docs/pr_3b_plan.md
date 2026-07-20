# Implementation Plan: PR 3B — Pure MTS Lifecycle Adapter (ACL)

This plan defines the architecture, data models, validation constraints, and commit sequence for implementing the **Pure MTS Lifecycle Adapter** (PR 3B). 

The primary objective is to establish a strict, testable, and deterministic **Anti-Corruption Layer (ACL)** between the runtime trading environment (`StrategyContext`, `tmf_spread`) and the pure decision core (`evaluate_lifecycle_actions`).

---

## 1. Architectural Boundary & Responsibilities

The `MtsLifecycleAdapter` is a pure coordination and translation layer. 

### 1.1 In-Scope Responsibilities
1.  **Translation**: Converts runtime state variables (`MtsStrategyState`, `MarketEvent`) into immutable `LifecycleContext` and `PositionLifecycle` structures.
2.  **Validation**: Validates temporal sequence, data completeness, and provenance of events.
3.  **Coordination**: Evaluates context through `evaluate_lifecycle_actions()` and packages outputs into structural results.

### 1.2 Out-of-Scope Responsibilities (Strictly Forbidden)
*   Modifying strategy state variables directly.
*   Writing files or interacting with persistence/database.
*   Submitting orders or communicating with the broker.
*   Determining priority of actions.
*   Inferring/guessing historical extrema from current mark price alone when provenance is missing.

---

## 2. Core Data Shapes & Interfaces

```python
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

class ExecutionMode(Enum):
    LIVE = "LIVE"
    PAPER = "PAPER"
    BACKTEST = "BACKTEST"

class RecoveryStatus(Enum):
    EXACT = "EXACT"
    PERSISTED = "PERSISTED"
    REPLAYED = "REPLAYED"
    DEGRADED = "DEGRADED"
    UNRECOVERABLE = "UNRECOVERABLE"

@dataclass(frozen=True)
class LifecycleEvaluationInput:
    strategy_state: dict
    market_event: dict
    lifecycle: PositionLifecycle
    execution_mode: ExecutionMode

@dataclass(frozen=True)
class LifecycleDiagnostics:
    build_status: str
    rejection_reason: str | None
    latency_ms: float

@dataclass(frozen=True)
class LifecycleEvaluationResult:
    context: LifecycleContext
    decision: LifecycleDecision | None
    diagnostics: LifecycleDiagnostics
```

---

## 3. Provenance Contract Guards

### 3.1 Temporal Boundaries
The Adapter enforces strict inequalities based on event timestamps (using `event_time`, not processing/received times):
$$\text{market\_event.event\_time} \ge \text{lifecycle.single\_leg\_started\_at}$$
$$\text{market\_event.event\_time} \ge \text{lifecycle.last\_applied\_event\_time}$$
$$\text{fill\_event.event\_time} \ge \text{release\_order\_submitted\_at}$$

Any event violating these invariants or containing out-of-order timestamps will produce structured rejection codes (`CLOCK_REGRESSION`, `PRE_SINGLE_LEG_EVENT`, etc.) rather than silent failures.

### 3.2 Two-Stage Anchoring
The adapter explicitly separates provisional order submission from permanent execution fills:
1.  **Release Order Submitted**: Emits `PRE_RELEASE_REFERENCE_CAPTURED` containing `pre_release_reference_price` and `pre_release_reference_time`. This is used solely for slippage analysis and diagnostics. Trailing stops **must not** be evaluated or initialized here.
2.  **Release Fill Confirmed**: Emits `POST_FILL_TRAIL_ANCHOR_SET` at `release_fill_event_time`. This sets `single_leg_started_at`, initializes peak/nadir to the remaining leg's first post-fill tick, and activates trailing stop warmup guards.

---

## 4. Restart-Aware Recovery

To prevent ad-hoc file scanning inside the adapter, the adapter is fed explicit facts:

```python
@dataclass(frozen=True)
class LifecycleRecoveryFacts:
    persisted_lifecycle: PositionLifecycle
    release_fill: dict | None
    remaining_position: dict | None
    persisted_extrema: dict | None
    latest_valid_market_event: dict | None

@dataclass(frozen=True)
class RecoveredLifecycleProjection:
    lifecycle: PositionLifecycle
    peak: float | None
    nadir: float | None
    recovery_status: RecoveryStatus
    evidence: list
```

### Recovery Precedence:
1.  Committed lifecycle state in persistence.
2.  Broker-confirmed fills log.
3.  Active broker positions.
4.  Append-only trade facts ledger.
5.  Market event extrema evidence.
6.  *No guessing or wall-clock fallbacks are allowed.*

---

## 5. Structural Parity & Validation Fingerprints

To declare the refactor successful, the Adapter must demonstrate **100% Action and State Parity** against the legacy inline evaluation code. 

### Parity Fingerprint Definition:
```python
parity_fingerprint = (
    action,
    target_leg,
    reason,
    priority,
    order_side,
    trigger_source,
    lifecycle_phase,
    threshold_snapshot,
)
```

---

## 6. Target Commit & Push Sequence

To ensure safety and ease of reversion, the implementation must be committed in sequential layers:

1.  `contracts`: Introduce adapter interfaces, data types, and diagnostic envelopes.
2.  `shadow`: Wire the adapter in "Shadow Mode" alongside the legacy inline path, logging comparisons and verifying parity without controlling live exits.
3.  `guards`: Apply the strict temporal invariants and two-stage anchoring validations.
4.  `recovery`: Introduce recovery projection types and precedence-based state reconstruction.
5.  `switch`: Swap the production evaluation path to the adapter.
6.  `cleanup`: Remove the legacy inline `_ctx2` construction and duplicate evaluations from `tmf_spread.py`.

---

## 7. Acceptance Criteria

The final validation gate requires 100% match on the following metrics across all historical test files:
1.  **Event Count Parity = 100%** (Total count of replayed market events matches exactly).
2.  **Build Status Parity = 100%** (Context builds and rejections occur at identical event indices).
3.  **Decision-Present Parity = 100%** (Decisions and non-decisions occur at identical event indices).
4.  **Fingerprint Parity = 100%** (Decision attributes, targets, and snapshot metrics match exactly).
5.  **First-Decision Index Parity = 100%** (The very first exit decision triggers at the same sequence index).
6.  **Decision Multiplicity Parity = 100%** (No extra or duplicated decisions are emitted).
7.  **Safety Invariant**: `received_at` and `processed_at` do not affect lifecycle decisions.
8.  **Rejection Safety**: Any adapter context rejection has an explicit orchestration policy and is never silently swallowed.
