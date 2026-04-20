---
phase: 1
slug: lifecycle-truth-contract
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-20
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 |
| **Config file** | `pytest.ini` |
| **Quick run command** | `python3 -m pytest tests/test_order_lifecycle -v` |
| **Full suite command** | `python3 -m pytest tests/ -v` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python3 -m pytest tests/test_order_lifecycle -v`
- **After every plan wave:** Run `python3 -m pytest tests/ -v`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 1-01-01 | 01 | 1 | EXEC-01 | T-1-01 | intent/order/deal records stay linked by local and broker IDs | unit/integration | `python3 -m pytest tests/test_order_lifecycle/test_contract_traceability.py -v` | ❌ W0 | ⬜ pending |
| 1-02-01 | 02 | 1 | EXEC-02 | T-1-02 | accepted/partial/fill/cancel/reject remain distinct lifecycle states | unit | `python3 -m pytest tests/test_order_lifecycle/test_order_manager.py -v` | ✅ partial/cancel/reject only | ⬜ pending |
| 1-03-01 | 03 | 1 | EXEC-03 | T-1-03 | position and cost basis mutate only from confirmed deal handling | integration/system | `python3 -m pytest tests/test_order_lifecycle/test_position_apply_on_confirmed_deal.py -v` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_order_lifecycle/test_contract_traceability.py` — cover linked `intent_id`, `order_id`, `deal_id`, and broker ID attachment
- [ ] `tests/test_order_lifecycle/test_position_apply_on_confirmed_deal.py` — block optimistic mutation on submit and prove confirmed-deal-only application
- [ ] Expand existing lifecycle tests to cover futures live order-state callbacks separately from deal callbacks

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| No manual-only behaviors planned | N/A | Current phase should be automatable with regression coverage | N/A |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
