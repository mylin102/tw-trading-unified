# ADR-024: Policy J Combined Exit Execution, Idempotency, and Legging Defense Governance

- **Status**: Conditionally Accepted (Pending First Live Golden Trace)
- **Date**: 2026-07-27
- **Author**: Gemini CLI & Engineering Team
- **Context**: MTS Spread Trading Execution & Risk Governance

---

## 1. Context & Architecture Audit

During live soak testing of Policy J (Combined UPL Trailing Exit), a critical dispatch routing defect was identified:
- **Routing Defect**: Policy J evaluates UPL during the `SPREAD` phase (both legs open) and produces `Signal(action="EXIT", reason="TMF_COMBINED_EXIT")`. However, the legacy `monitor.py` dispatcher enforced a strict Phase Isolation Guard (`phase == SINGLE_LEG`), causing `COMBINED_EXIT` signals to be blocked with `MTS_EXIT_BLOCKED_PHASE_ISOLATION`.

To safely open a combined dual-leg exit execution channel without compromising systemic trading security, an architectural audit identified 10 critical execution governance requirements:

1. **Routing Position & Matching**: `Signal(action="EXIT", reason="TMF_COMBINED_EXIT")` must be intercepted by the `COMBINED_EXIT` handler *before* generic single-leg phase isolation checks, while keeping generic `EXIT` signals strictly blocked in `SPREAD` phase.
2. **Legging Risk Recognition**: Dual `create_order()` calls are executed sequentially within a single dispatcher invocation and carry intrinsic legging exposure (`Near accepted / Far rejected`, `Near filled / Far pending`).
3. **Fail-Closed Side Mapping**: Abolish silent `else BUY / SELL` fallbacks. Position sides must be strictly mapped via `CLOSE_SIDE = {"LONG": OrderSide.SELL, "SHORT": OrderSide.BUY}`. Any missing or unrecognized side raises `INVALID_POSITION_SIDE` and halts execution immediately.
4. **Dynamic Quantity Derivation**: Quantities must be dynamically derived from strategy/broker position state (`_near_qty`, `_far_qty`, `_lots`). Hardcoded `quantity=1` is strictly prohibited.
5. **Trade-Level Execution Idempotency**: Atomic claim key (`{trade_id}:POLICY_J:COMBINED_EXIT`) prevents duplicate order submissions across fast tick arrivals prior to fill callbacks.
6. **Legging Failure Matrix & Remediation**: The system tracks 4 fill outcomes (`accepted/accepted`, `accepted/rejected`, `rejected/accepted`, `rejected/rejected`), setting dedicated state (`COMBINED_EXIT_PARTIAL` / `EMERGENCY_SINGLE_LEG`) rather than marking position flat prematurely.
7. **Fill-Driven State Transitions**: Position state remains `has_position = True` until confirmed broker fills callbacks process both legs flat.
8. **Execution Telemetry Correlation**: Event schema tracks `execution_id`, `idempotency_key`, `attempt_no`, `leg_role`, `exit_reason`, and `broker_order_id`.
9. **Dedicated 15-Case Execution Test Suite**: Certified by `tests/strategies/test_policy_j_combined_exit_execution.py`.
10. **Governance Promotion Contract & Rollback Conditions**: Formalizes Shadow-to-Live promotion parameters and automatic rollback triggers.

---

## 2. Decision Outcomes & Governance Rules

### 2.1 Routing & Side Fail-Closed Standard
In `strategies/futures/monitor.py`:
```python
elif _action == "COMBINED_EXIT" or _reason in ("TMF_COMBINED_EXIT", "COMBINED_EXIT"):
    _claim_key = f"{_trade_id}:POLICY_J:COMBINED_EXIT"
    if _claim_key in self._claimed_execution_keys:
        console.print("[COMBINED_EXIT_DUPLICATE_SUPPRESSED]")
        return

    CLOSE_SIDE = {"LONG": OrderSide.SELL, "SHORT": OrderSide.BUY}
    if _near_side_str not in CLOSE_SIDE or _far_side_str not in CLOSE_SIDE:
        console.print("[MTS_COMBINED_EXIT_BLOCKED] INVALID_POSITION_SIDE")
        return
```

### 2.2 Legging Remediation Policy
When a dual-leg exit experiences partial failure:
1. **Detection**: Position stays `has_position = True` and transitions to `COMBINED_EXIT_PARTIAL`.
2. **Controlled Retry**: Bounded retry for the remaining un-filled leg using latest broker quote.
3. **Emergency Escalation**: If retry fails or times out, transition to `EMERGENCY_SINGLE_LEG` and trigger operator alert.

### 2.3 Automatic Rollback Conditions
Policy J execution is automatically suspended (`enable_combined_upl_trail = False`) if any of the following occur:
- Any duplicate order submission escape.
- Any un-hedged single-leg exposure exceeding 30 seconds.
- Any unresolved broker/local position mismatch during combined exit.
- Any restart failure during pending combined exit.

---

## 3. Current Governance & Verification Status

```text
ADR-024 Architecture:                  ACCEPTED
Combined Exit Dispatcher Routing:      VERIFIED (15/15 passed)
Side Fail-Closed:                      VERIFIED
Dynamic Quantity Derivation:           VERIFIED
Execution Idempotency:                 IMPLEMENTED
Fill-Driven Settlement:                VERIFIED
Partial Exit Detection:                VERIFIED
Partial Exit Remediation:              SPECIFIED & IMPLEMENTED
Restart State Persistence:             VERIFIED BY TEST
Regression Suite (53/53):              100% PASSED 🟢
Git & PM2 Deployment Integrity:        VERIFIED (SHA 65909fe9)
Broker-Level Operational Closure:      PENDING FIRST LIVE GOLDEN TRACE
```

### 3.1 First-Live-Execution Evidence Gate Requirements
To reach full **Broker-Level Operational Closure**, the first real live `COMBINED_EXIT` trigger must produce a verified Golden Trace documenting:
1. `POLICY_J_TRIGGERED`
2. `EXECUTION_CLAIM_ACQUIRED`
3. `NEAR_ORDER_SUBMITTED` & `FAR_ORDER_SUBMITTED`
4. `NEAR_FILLED` & `FAR_FILLED`
5. `BROKER_POSITIONS_RECONCILED_ZERO`
6. `PNL_SETTLED_ONCE`
7. `LIFECYCLE_FLAT`
