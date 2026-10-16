# Task 1.3.2 integration evidence

2026-10-15 Hermes Agent: integrate the user's `/tmp/codex-task-1.3.2-dispatch.md` without changing supplied implementation logic.

Task 1.3.2 is implemented locally. The supplied unit suite passes 16/23, meeting the dispatch's `> 15/23` threshold. Seven supplied-suite failures remain and are recorded below; this is not an all-tests-passing or deployment claim.

## Target and scope

- Hostname: `MacBook-Air-2.local`; ComputerName: `MacBook Air (4)`; architecture: `arm64`.
- deployment_id: `air4`; host_role: `offline_research`.
- instance_id: `mts-oi-vol-integration-20261015`; ticker: `TMF`; permitted execution mode: `paper`.
- Resolved repository: `/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015`.
- Branch: `feat/mts-oi-vol-integration-20261015`.
- Baseline commit: `21d5f9a368ed71a0345c5d24522c1335511cf4b7`; tracked working tree clean before this task.
- Operation: offline integration/tests. No broker access, cron changes, PM2 operations, runtime start, Mini synchronization, push, or PR creation.
- Prior OI / Vol modules are unchanged. Existing OI performance and event-schema promotion blockers remain.

## Implementation

- Copy `mts_vol_quality.py` byte for byte from Downloads. SHA-256 for both copies: `3fd520443b1bcbdb8c8d45a697cca88ad47fec45d0c69ae4e5db7b0b359c93d3`.
- Add `vol_quality_score: Optional[float] = None` and `vol_quality_level: Optional[str] = None` to the actual project dataclass, `HvwapCandidateVerdict`. Existing `asdict()` serialization includes them automatically.
- Call the supplied `VolQuality.compute()` from `evaluate_hvwap_candidate()`, using OI's completed-bar timestamp, deduplicated entry-plus-exit order count, fill count, and matching event window.
- `TMFSpread` derives `(completed_close - 300, completed_close]` events from the estimator's already-loaded event cache. It reuses the same mode-scoped writer path and per-completed-bar cache key, excluding invalid timestamps, the lower boundary, and future events. No extra log-file read is added by quality integration.
- The new event-derived quality score remains separate from existing Z-score `vol_quality`. Optional input absence or quality exceptions leave the two new fields null; quality exceptions are logged. Arbitration and release behavior remain unchanged.
- The existing internal estimator cache is used because the supplied estimator has no public event-window accessor; its implementation was not modified.

## Validation

Interpreter: `/Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3`. Pytest commands run with `taskpolicy -c background`.

| Check | Result |
| --- | --- |
| Supplied Quality unit suite before integration | 16 passed, 7 failed, 23 collected |
| Supplied Quality unit suite after integration | 16 passed, same 7 failed |
| New Task 1.3.2 integration tests | 7 passed |
| Existing OI / Vol integration tests | 23 passed |
| Existing targeted strategy/release regression tests | 45 passed, same 2 baseline failures |
| Combined focused run | 91 passed, 9 evidenced baseline failures, 100 collected |
| Five fraud indicators | Spy coverage confirms stacking, density, time_cluster, imbalance, follow_through all execute via the candidate call |
| Optional fields | Both have Optional types and None defaults; serialization verified |
| Syntax compilation | Three affected production modules passed |
| Whitespace check | Integration edits pass; full staged diff reports 13 trailing-whitespace lines inherited byte for byte from supplied `mts_vol_quality.py`; preserved per dispatch |
| Bounded full-suite attempt (`tests/ --maxfail=1`) | 4538 collected; 30 passed, 1 failed; stopped on `test_unified_config_session_override_merging` (89.47s); full suite not completed |

Focused test paths:

```text
tests/test_mts_vol_quality.py
tests/test_mts_vol_quality_integration.py
tests/test_mts_oi_vol_integration.py
tests/strategies/test_mts_release_opening_window.py
tests/strategies/test_mts_trend_wiring.py
tests/contracts/test_hvwap_release_intent_event_schema.py
```

The first broad-suite blocker was independently reproduced in the unchanged prior worktree `tw-trading-unified-deploy-cli-20261015` (1 passed, 1 failed in the config test file).

The three production-file scope warrants a broad attempt, but this checkout already has documented broad-suite failures. `--maxfail=1` records the first blocker without repeating the previous long, stalled full run. Supporting tests do not relax production failures. No supplied test expectations were changed, other than package imports and attribution needed to integrate the test file.

## Supplied-suite issues retained

| Test | Supplied actual result | Expected |
| --- | --- | --- |
| follow_through_low | 92.857 at 20% fill | 100 |
| follow_through_middle | 50 at 50% fill | Between 55 and 65 |
| quality_score_good | 97.5 / EXCELLENT | Between 70 and 85 / GOOD |
| quality_score_suspicious | 16 / FRAUDULENT | Between 50 and 70 / SUSPICIOUS |
| quality_score_no_fills | 30 / FRAUDULENT | Strictly below 30 |
| scenario_real_spike | 52.5 / SUSPICIOUS | Between 60 and 80 |
| performance_1000_calculations | 0.866s baseline, 0.985s after | Below 0.5s |

Several differences exceed the dispatch's anticipated +/-5-point tolerance. They are pre-integration discrepancies, not Optional-field/schema failures. The two existing targeted regression failures are the retained-direction expectation in `test_counter_trend_leg_mapping` and missing baseline `gate_reason` in the release-intent emission schema test.

Current event producers still lack the supplied estimator's required `order_id` and the quality module's lowercase `side` fields in some event forms. Thus these scores are observational outputs of the supplied algorithms; live-data validity and prior performance blockers remain unresolved. No event identities/sides were fabricated.

## Local evidence

- `/tmp/mts-vol-quality-module-baseline.log`
- `/tmp/mts-vol-quality-integration-baseline.log`
- `/tmp/mts-vol-quality-after.log`
- `/tmp/mts-vol-quality-full-attempt.log`
- `/tmp/mts-vol-quality-full-baseline-config.log`

Synchronization: NOT_SYNCHRONIZED. Runtime: RUNTIME_NOT_VERIFIED. Local source integration is reviewable; no production promotion is claimed.
