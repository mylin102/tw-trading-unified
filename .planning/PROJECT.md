# Project: tw-trading-unified GSD Optimization Plan

## Guiding Principle

This plan prioritizes **capital protection and runtime determinism first**.
For this repository, "optimization" means:

1. Prevent live/paper trading regressions that can create financial loss
2. Make runtime failures easy to detect and localize
3. Extract only the highest-risk responsibilities into testable units
4. Defer infra migration and broader architecture cleanup until the trading path is boring

The system follows:
- **GSD**: Discover → Plan → Execute → Verify → Ship
- **SDD**: Single source of truth, side effects after validation, defensive programming
- **V-Model**: Unit, integration, system, and paper-trading validation before promotion

---

## Wave 0: Freeze Safety Invariants

### Status
Completed and validated on 2026-04-10.

### Goal
Lock in the exact trading rules that must never regress before any runtime or architecture work.

### In Scope
- Add or tighten regression tests for:
  - duplicate entry prevention
  - same-bar exit then re-entry block
  - partial exit / TP1 recovery
  - fee / exchange fee / tax-inclusive PnL
  - session rollover and trading-day mapping
  - stale contract / stale tick handling
  - paper capital / margin rejection
- Tie tests directly to [RULES.md](../RULES.md) invariants

### Out of Scope
- Refactors to monitor class structure
- GCP migration
- Stock module service separation
- Strategy plugin redesign

### Risks
- Existing test coverage may miss live-path edge cases
- Current collection failures may block full-suite confidence until fixed separately

### Tests / Validation
- `python3 -m pytest tests/ -v`
- Targeted regression tests for:
  - `tests/test_trading_bugs.py`
  - `tests/test_data_chain.py`
  - new stale-contract and session-rollover coverage if missing

### Exit Criteria
- Every critical trading invariant has an automated regression test
- A future refactor cannot silently reintroduce repeat-entry or fee-accounting bugs

### Validation Notes
- Added test-bootstrap hardening for environment-specific import failures:
  - Numba `cache=True` disabled during pytest collection only
  - Shioaji log path redirected to `/tmp` during tests only
- Locked options stale-contract handling:
  - single `_check_options_contract_staleness()` implementation only
  - no local tick re-subscribe path for valid-but-quiet contracts
- Locked date/session invariants:
  - vectorized `get_trading_day()` preserves pandas alignment
  - vectorized `get_session_date_str()` preserves pandas alignment
  - holiday override pushes to next valid trading day
  - scalar/vectorized day-night boundaries covered
- Fixed stock pattern regression:
  - deep handle no longer misclassified as cup bottom in `cup_with_handle`
- Validation gate:
  - `python3 -m pytest tests/ -v`
  - result at checkpoint: `184 passed`

---

## Wave 1: Runtime Stabilization

### Goal
Make futures/options runtime behavior deterministic under stale data, contract rollover, and session transitions.

### In Scope
- Resolve conflicting stale-contract handling in:
  - `strategies/options/live_options_squeeze_monitor.py`
  - `strategies/futures/monitor.py`
- Standardize one canonical policy for:
  - when to warn only
  - when to refresh contracts
  - when to resubscribe
  - when to escalate to sentinel restart
- Lock return types and edge-case behavior in:
  - `core/date_utils.py`
- Ensure dashboard and monitors interpret session/trading-day logic identically

### Out of Scope
- Large-scale class extraction
- New strategy development
- Config redesign beyond runtime safety needs

### Risks
- Options/futures can diverge again if contract refresh semantics are not unified
- Date/session helpers can create silent downstream regressions if pandas/list/scalar behavior is inconsistent

### Tests / Validation
- `python3 -m pytest tests/ -v`
- Dry-run startup of `main.py`
- Validation of:
  - contract selection logs
  - rollover behavior
  - stale-tick escalation path
  - session-date consistency between writer and dashboard reader

### Exit Criteria
- No duplicate or contradictory stale-handling logic remains
- Session/date behavior is deterministic and test-locked
- Runtime recovery behavior is documented and observable in logs

---

## Wave 2: Minimal Service Extraction

### Goal
Reduce monitor complexity without destabilizing the profitable trading path.

### In Scope
- Extract only the highest-risk cross-cutting responsibilities into dedicated services:
  - `SessionClock`
  - `ContractResolver`
  - `TradeLogger`
- Define for each service:
  - input contract
  - output contract
  - side-effect ownership
  - single source of truth
- Keep monitor classes as orchestration layers

### Out of Scope
- Full monitor rewrite
- Execution-engine rewrite
- Indicator-engine rewrite
- Multi-strategy plugin generalization

### Risks
- Half-refactors can create two sources of truth
- Logging extraction is dangerous if it changes the order of state reset vs persistence

### Tests / Validation
- `python3 -m pytest tests/ -v`
- Focused integration tests around:
  - entry path
  - exit path
  - logger ordering
  - position recovery

### Exit Criteria
- Side effects are localized
- Position state ownership is unchanged and explicit
- Monitor classes are smaller and easier to inspect without changing trading semantics

---

## Wave 3: Proven Path Parity

### Goal
Align the live and backtest behavior of the one strategy path already shown to have edge.

### In Scope
- Prioritize `counter_vwap` parity between:
  - live futures monitor
  - paper trading
  - backtest path
- Align:
  - signal contract
  - stop-loss semantics
  - break-even / trailing behavior
  - fee/tax accounting
  - entry/exit guards

### Out of Scope
- Making every legacy strategy plugin-compliant
- Broad strategy expansion
- Tuning weak or unvalidated strategies first

### Risks
- Portability work can consume time without improving PnL or safety
- Backtest/live drift may remain hidden if only config is aligned but semantics are not

### Tests / Validation
- `python3 -m pytest tests/ -v`
- Compare same-signal scenarios across backtest and paper paths
- Review discrepancy cases explicitly instead of averaging them away

### Exit Criteria
- The profitable path behaves consistently across backtest and paper modes
- Deviations are documented and intentional, not accidental

---

## Wave 4: Observability and Operations

### Goal
Make failures cheap to diagnose before expanding deployment scope.

### In Scope
- Add explicit health signals for:
  - futures `last_tick_at`
  - options `last_tick_at`
  - current subscribed contract codes
  - last successful order timestamp
  - last blocked-entry reason
  - last recovery source
- Surface these in readiness/dashboard views
- Separate operational failure domains after runtime is stable:
  - stock runner isolation
  - service supervision improvements

### Out of Scope
- Full cloud migration in the same wave
- New trading features

### Risks
- Generic "stale" alerts are not actionable enough in a live trading system
- Service separation before observability can make debugging harder, not easier

### Tests / Validation
- `python3 -m pytest tests/ -v`
- Manual dry-run verification of health/readiness panels
- Simulated stale-data and blocked-entry scenarios

### Exit Criteria
- An operator can determine within one minute whether failure is in:
  - futures data
  - options data
  - contract selection
  - order path
  - sentinel/recovery flow

---

## Deferred Milestones

These remain important, but are intentionally deferred until Waves 0-4 are complete:

### Milestone A: Infrastructure and Deployment
- GCP VM rollout
- secrets hardening
- process supervision / service management

### Milestone B: Service Isolation
- stock runner separation
- fault-domain isolation between stocks and futures/options

### Milestone C: Signal Source Integration
- watchlist JSON contract
- dynamic watchlist loading

---

## Mandatory Gates

Every implementation wave must include:

1. **Pre-flight**
   - Confirm target invariant or failure mode
   - Confirm owning files and single source of truth

2. **Execution**
   - Minimal diff
   - No hidden side-effect reordering

3. **Verification**
   - `python3 -m pytest tests/ -v`
   - syntax / startup verification when runtime files are touched

4. **Promotion**
   - Dry-run first
   - Paper-trading observation before any config promotion

---

## Current Priority

### Primary Next Step
Execute **Wave 0** and the first half of **Wave 1**:
- freeze safety invariants with regression tests
- resolve stale-contract logic conflicts
- lock session/date helper behavior

### Why This First
This is the highest risk-reduction per line changed.
It reduces the chance of financial-loss regressions before larger refactors or infrastructure work.
