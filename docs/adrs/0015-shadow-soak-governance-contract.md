# ADR-0015: Shadow Soak Governance Contract & Release Candidate Baseline

**Status**: ACCEPTED  
**Date**: 2026-07-24  
**Author**: Gemini CLI & Quantitative Trading System Architecture Team  
**Scope**: MTS Multi-Exit Strategy Refactoring Governance (`Wave 1D.3` → `Wave 1E`)  

---

## Context & Problem Statement

In Wave 1C and Wave 1D.1/1D.2, the pure無狀態 `NormalReleasePolicy` and `ProcessSafeTelemetryLogger` were extracted, unit-tested, and verified on Mini (91/91 tests PASSED). 

To prevent "Context Rot" and logic drift during production observation, we must establish a formal **Shadow Soak Governance Contract** freezing the Release Candidate baseline (`Wave 1D.3 RC1`, Commit `fd5d5022`). Zero strategy or collector feature code changes are permitted during observation.

---

## Governance Rules & Invariants

### 1. Generation Start Conditions
A Shadow Soak Generation MAY ONLY START when all preflight gates pass:
- `authority == "legacy"` (sole production decision maker).
- `git_clean_status == True` (`git status --porcelain` is empty).
- `HEAD == origin/master` (local deployment matches remote tracking commit).
- `spool directory writable` with exclusive directory creation (`path.mkdir(exist_ok=False)`).

### 2. Generation Invalidation Conditions (`INVALID` State)
A Generation is IMMEDIATELY INVALIDATED (`status = INVALID`, `promotion_eligible = False`) if any of the following occur:
- Git commit or code changed during observation.
- Working tree becomes dirty.
- Configuration YAML or schema changes.
- `authority` is switched away from `"legacy"`.
- Generation directory ID collides or is reused.
- Raw telemetry files are corrupted or unaccounted process loss occurs.

### 3. Generation Four-State Outcomes
1. **`PASS`**: All promotion gates, non-interference counters, and accounting equations pass 100%.
2. **`FAIL`**: Unexplained mismatch or non-interference violation (`shadow_caused_orders > 0`).
3. **`INCOMPLETE`**: Insufficient day/night session coverage or missing lifecycle events.
4. **`INVALID`**: Evidence cannot be trusted due to preflight failure, dirty tree, or corrupted telemetry.

### 4. Promotion Contract Rule
> **Wave 1E (Authority Switch) SHALL NOT START unless `ShadowSoakManifest.result == "PASS"`.**

---

## Release Candidate Baseline

- **Frozen Release Candidate**: `Wave 1D.3 RC1` (Commit `fd5d5022`)
- **Authority**: `authority = "legacy"` (100% authoritative)
- **Shadow Authority**: `authority = "none"` (0% order/state mutation)

---

## Consequences

- **Positive**: Guarantees zero decision drift, provides deterministic evidence auditing, and prevents premature authority switching.
- **Negative**: Requires restarting a brand-new generation if any code modification occurs during soak.
