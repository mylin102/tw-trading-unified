# ADR-0015: Shadow Soak Governance Contract & 9-Gate Promotion Protocol

**Status**: ACCEPTED  
**Date**: 2026-07-24  
**Author**: Gemini CLI & Quantitative Trading System Architecture Team  
**Scope**: MTS Multi-Exit Strategy Refactoring Governance (`Wave 1D.3` → `Wave 1E`)  

---

## Context & Problem Statement

To transition safely from `Wave 1D.3` (Production Observation) to `Wave 1E` (Runtime Authority Switch), we establish a formal, independent **9-Gate Promotion Protocol**. An **Independent Acceptance Verifier** (`strategies/futures/mts/acceptance_verifier.py`) parses raw telemetry files and manifest digests directly from disk to prevent collector self-assessment bias.

---

## The 9 Standard Acceptance Gates (G1 to G9)

| Gate ID | Gate Name | Requirement / Formula | Pass Condition | Status Outcome |
| :--- | :--- | :--- | :---: | :---: |
| **G1** | **Baseline Provenance & Preflight** | `git_commit == expected_rc_commit` $\land$ `git_clean_status == True` $\land$ `authority == "legacy"` | 100% | `INVALID` if fails |
| **G2** | **Runtime Non-Interference** | $\text{orders} = 0 \land \text{commits} = 0 \land \text{appends} = 0 \land \text{dup\_legacy} = 0 \land \text{dup\_shadow} = 0 \land \text{unclassified} = 0$ | 100% | `FAIL` if fails |
| **G3** | **Evaluation Accounting** | $\text{cycles\_seen} = \text{matches} + \text{mismatches} + \dots + \text{context\_build\_failed}$ | 100% | `FAIL` if fails |
| **G4** | **Delivery & Reconciliation** | $\text{enqueued} = \text{written} + \text{dropped} + \text{pending}$ $\land$ $\text{pending} = 0$ $\land$ $\text{cycles\_seen} = \text{raw\_records} + \text{dropped}$ | 100% | `FAIL` if fails |
| **G5** | **Zero Decision Mismatch** | $\text{mismatches} = 0 \land \text{unexplained\_mismatches} = 0$ (Zero Waivers Allowed) | 100% | `FAIL` if fails |
| **G6** | **Minimum Session Coverage** | $\text{cycles} \ge 200 \land \text{day} \ge 100 \land \text{night} \ge 100 \land \text{lifecycles} \ge 5$ | 100% | `INCOMPLETE` if fails |
| **G7** | **Controlled Restart Continuity**| $\text{process\_segments} \ge 2 \land \text{restart\_reconciliations} \ge 1$ | 100% | `INCOMPLETE` if fails |
| **G8** | **Performance & Latency Budget** | $\text{shadow\_eval\_p99\_us} \le 100.0\mu s \land \text{overflow\_rate} = 0.0$ | 100% | `FAIL` if fails |
| **G9** | **SHA-256 Digest & Raw Integrity**| Python `sha256(manifest.json) == manifest.sha256` | 100% | `INVALID` if fails |

---

## Promotion Contract Rule

> **Wave 1E (Runtime Authority Switch) SHALL NOT START unless `AcceptanceReport.overall_status == "PASS"`.**

---

## Four-State Outcome Definitions

1. **`PASS`**: All 9 gates G1–G9 pass 100%. Unlocks Wave 1E.
2. **`FAIL`**: Decision mismatch, non-interference violation, or performance budget breach occurs.
3. **`INCOMPLETE`**: Insufficient session/lifecycle coverage or controlled restart evidence not observed.
4. **`INVALID`**: Evidence cannot be trusted (git dirty, commit mismatch, SHA-256 digest corrupted, or raw loss).

---

## Consequences

- **Positive**: Eliminates collector self-assessment bias through an independent cross-platform Python verifier (`acceptance_verifier.py`).
- **Negative**: Requires strict adherence to all 9 gates before authority switch.
