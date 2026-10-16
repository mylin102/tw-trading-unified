# Task 1.1.2 + 1.2.2 integration evidence

2026-10-15 Hermes Agent: implement the user's `/tmp/codex-task-1.1.2-1.2.2-dispatch.md` on Air4.

Status: MODIFIED_ON_AIR4. Integration is implemented; acceptance remains blocked by supplied-module test/performance and event-schema issues. No push, PR, Mini synchronization, cron change, broker access, or PM2 restart performed.

## Target provenance

- Hostname: `MacBook-Air-2.local`; ComputerName: `MacBook Air (4)`; architecture: `arm64`.
- deployment_id: `air4`; host_role: `offline_research`.
- instance_id: `mts-oi-vol-integration-20261015`.
- Resolved repository: `/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015`.
- Branch: `feat/mts-oi-vol-integration-20261015`.
- Baseline: `44bde397b4bb8128d948e135cbe612f73b6c84c4` (common integration baseline, before unrelated deployment CLI work).
- Ticker: `TMF`; permitted execution mode: `paper`; operation: offline integration tests only.
- Initial tracked working tree was clean. The local `.deployment-target` was created for this instance and is ignored by Git.
- Mini synchronization: NOT_SYNCHRONIZED. Runtime: RUNTIME_NOT_VERIFIED; no deployment requested.

## Implementation

The supplied project has `HvwapCandidateVerdict` and `evaluate_hvwap_candidate()`, rather than the dispatch example's `HvwapCandidate` and class method. The existing interface was extended without renaming it.

- Copy both supplied production modules byte for byte.
- Add `oi_intensity`, `oi_direction`, `oi_quality`, `vol_zscore`, `vol_anomaly_level`, and `vol_quality`, all `Optional[...] = None`. They are automatically included by the existing `asdict()` serialization. There is no `__post_init__` or separate mandatory candidate schema to update.
- Keep event-log IO in `TMFSpread`, passing an optional `OISnapshot` into the pure candidate evaluator.
- Read the same `_MTS_EVENT_LOG` path as the writer, preserving runtime mode/path resolution. Invalidate the integration cache when this path changes.
- Estimate OI once per completed bar close, rather than rescanning the log on every tick. Use the completed bar's timestamp for the estimator's as-of window.
- Evaluate VolAdjuster using the latest completed current-session NEAR bar's own volume and close timestamp; never use pending or FAR volume. Unavailable inputs remain null. Off-segment adjustment may produce the supplied module's `UNKNOWN` value.
- Optional OI errors are logged and leave OI fields null while preserving the existing HVWAP verdict. Existing arbitration and release gates do not consume these six observation fields.

Supplied-module SHA-256 identity:

| File | Downloads and integrated SHA-256 |
| --- | --- |
| mts_oi_estimator.py | `2bc0db20b6d507e00878afb8ca5d8b676c62f92dd739e86f8f538e2e087c10ec` |
| mts_vol_adjuster.py | `7a9b022b503780b79779c76893ef9ce33ece22a3b1ff049419e353328d3e6f56` |

## Verification

Interpreter: `/Users/mylin/Documents/mylin102/tw-trading-unified-deploy-cli-20261015/venv/bin/python3`. Commands run with `taskpolicy -c background`, including child processes. Supplied tests were copied with only package imports and attribution added; their fixtures and expectations remain unchanged.

| Check | Result |
| --- | --- |
| Supplied OI tests | 16 passed, 4 failed, 20 collected |
| Supplied Vol tests | 31 passed, 3 failed, 34 collected (dispatch states 33) |
| New integration/default/schema tests | 23 passed |
| Existing targeted regressions | 45 passed, 2 failed; same two failures before integration |
| Integrated targeted run including new tests after dependency installation | 68 passed, 2 baseline failures |
| Production syntax compilation | All four modules passed |
| `git diff --check` | Passed |
| Full collection before dependency installation | Blocked by 10 import errors (`pydantic`, `ruamel`) |
| Full collection after dependency installation | 4508 collected; no collection errors |
| Full test execution after dependencies | Interrupted at approximately 26% after multiple failures and prolonged stalled progress; exit 143 after SIGTERM; full suite NOT completed or green |

The user's follow-up authorized installing `pydantic`, `ruamel.yaml`, and `pytest`. Installed versions: `2.14.1`, `0.19.1`, `9.0.3` respectively. The target virtualenv's pip lacked its bundled certificate; system pip's `--python` option installed into the same test virtualenv successfully. No project dependency manifest was changed.

Evidence logs (local test outputs, not runtime artifacts):

- `/tmp/mts-oi-vol-baseline-regression.log`
- `/tmp/mts-oi-vol-baseline-collection.log`
- `/tmp/mts-oi-vol-after.log`
- `/tmp/mts-oi-vol-regression-with-deps.log`
- `/tmp/mts-oi-vol-collection-with-deps.log`
- `/tmp/mts-oi-vol-full-with-deps.log`

## Remaining issues / acceptance blockers

1. OI `test_oi_direction_all_new_orders` and `test_oi_direction_all_close_orders` put the second event at 10:01 but evaluate at 10:00; expecting two orders conflicts with as-of filtering.
2. OI `test_window_boundary_inclusive` expects the lower boundary to be included, while the supplied estimator explicitly uses `(decision_ts - window_secs, decision_ts]`. Actual count is two, expected three.
3. OI 100k-event performance fails the 500ms requirement: 117.423s before integration and 110.621s after integration in this local background execution environment. No internal optimization performed. Even with per-bar caching, the first computation can block the strategy tick; this implementation is not ready for runtime promotion.
4. Vol `test_adjust_high_anomaly_morning`: volume 588 gives z=1.0, classified NORMAL by the documented `z <= 1` rule, while the test expects HIGH.
5. Vol `test_adjust_different_segments`: afternoon volume 450 gives `(450-396)/96 = 0.5625`, also NORMAL, while the test expects HIGH.
6. Vol 1000-calculation performance is 1.098s after integration versus the supplied 0.1s test requirement (baseline also failed).
7. Current `TMFSpread` ENTRY / ENTRY_AUDIT / EXIT_LOG event producers do not provide the estimator's required `order_id`. Such orders are ignored, so real activity can appear as zero OI intensity. An integration regression explicitly demonstrates this. No event producer or estimator internals were changed to invent order identities.
8. Existing `test_counter_trend_leg_mapping` fails independently of this change: SHORT/LONG mapping returns retained LONG, while the test expects SHORT. Existing release-intent schema test expects `gate_reason`, which the baseline emission lacks. These are not optional-field validation failures.
9. The broad full-suite run showed additional configuration, MTS entry/retry, execution-context, and live-route failures. They have not all been compared against baseline and must not be described collectively as pre-existing. The run stopped at approximately 26% after stalled progress; SIGINT did not end it, and SIGTERM terminated the identified test PID 27230. No trading process was targeted.
10. A malformed string volume can already raise in the existing bar aggregator before optional volume scoring; this unrelated baseline behavior was observed and left unchanged.

Reviewable integration is available now. Merge-ready acceptance requires corrected upstream implementation/test expectations and resolved OI event-schema/performance issues; no reliable ETA can be given before those inputs are supplied.
