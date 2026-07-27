# ADR-024: Policy J Combined Exit Execution, Idempotency, and Legging Defense Governance

- **Status**: Accepted
- **Date**: 2026-07-27
- **Author**: Gemini CLI & Engineering Team
- **Context**: MTS Spread Trading Execution & Risk Governance

---

## 1. Context & Architecture Review

During live soak testing of Policy J (Combined UPL Trailing Exit), a critical dispatch routing defect was identified:
- **Routing Defect**: Policy J evaluates UPL during the `SPREAD` phase (both legs open) and produces `Signal(action="EXIT", reason="TMF_COMBINED_EXIT")`. However, the legacy `monitor.py` dispatcher enforced a strict Phase Isolation Guard (`phase == SINGLE_LEG`), causing `COMBINED_EXIT` signals to be blocked with `MTS_EXIT_BLOCKED_PHASE_ISOLATION`.

To safely open a combined dual-leg exit execution channel without compromising systemic trading security, an architectural audit identified 10 critical execution governance requirements:

1. **Routing Position & Matching**: `Signal(action="EXIT", reason="TMF_COMBINED_EXIT")` must be intercepted by the `COMBINED_EXIT` handler *before* generic single-leg phase isolation checks, while keeping generic `EXIT` signals strictly blocked in `SPREAD` phase.
2. **Legging Risk Recognition**: Acknowledge that dual `create_order()` calls are executed sequentially in one dispatcher invocation and carry intrinsic legging exposure.
3. **Fail-Closed Side Mapping**: Abolish silent `else BUY / SELL` fallbacks. Position sides must be strictly mapped via `CLOSE_SIDE = {"LONG": OrderSide.SELL, "SHORT": OrderSide.BUY}`. Any missing or unrecognized side raises `INVALID_POSITION_SIDE` and halts execution immediately.
4. **Dynamic Quantity Derivation**: Quantities must be dynamically derived from strategy/broker position state (`_near_qty`, `_far_qty`, `_lots`). Hardcoded `quantity=1` is strictly prohibited.
5. **Trade-Level Execution Idempotency**: Atomic claim key (`{trade_id}:POLICY_J:COMBINED_EXIT`) prevents duplicate order submissions across fast tick arrivals prior to fill callbacks.
6. **Legging Failure Matrix**: The system tracks 4 fill outcomes (`accepted/accepted`, `accepted/rejected`, `rejected/accepted`, `rejected/rejected`), setting dedicated state (`COMBINED_EXIT_PARTIAL` / `EMERGENCY_SINGLE_LEG`) rather than marking position flat prematurely.
7. **Fill-Driven State Transitions**: Position state remains `has_position = True` until confirmed broker fills callbacks process both legs flat.
8. **Execution Telemetry**: Full telemetry lifecycle (`ORDER_SUBMITTED`, `COMBINED_EXIT_NEAR`, `COMBINED_EXIT_FAR`, `FINAL_EXIT`).
9. **Dedicated 15-Case Execution Test Suite**: Certified by `tests/strategies/test_policy_j_combined_exit_execution.py`.
10. **Governance Promotion Contract**: Formalizes Shadow-to-Live promotion parameters.

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

### 2.2 Shadow-to-Live Promotion Contract

| Parameter | Approved Value | Purpose |
|---|---|---|
| `enable_combined_upl_trail` | `true` | Enables Policy J tracking & combined exit |
| `combined_upl_activation_net_pnl_twd` | `200.0 TWD` | Activation net profit threshold |
| `combined_upl_giveback_twd` | `50.0 TWD` | Retracement giveback trigger threshold |
| `idempotency_claim_key` | `{trade_id}:POLICY_J:COMBINED_EXIT` | Trade-level duplicate submission suppression |
| `execution_mode` | `MKP (範圍市價)` | Slippage-controlled market range orders |

---

## 3. Verification & Compliance

1. **SQUEEZE_FIRE_SCOUT Regression**: `21 passed` 🟢
2. **Policy J Execution Suite**: `15 passed` (`tests/strategies/test_policy_j_combined_exit_execution.py`) 🟢
3. **UI & Reporting Governance**: `17 passed` 🟢
