# Task 1.4.2 delivery evidence

2026-10-16 Hermes Agent: Adaptive VWAP-TWAP implementation and paired collection.

Implementation is reviewable locally. Real trading improvement and Phase 2 promotion remain unverified. No push, Mini synchronization, broker access, cron change, PM2 operation or deployment was performed.

## Deliverables

- `mts_adaptive_vwap.py`: four-signal chips strength, validated configurable weights, stable sigmoid, completed-interval TWAP, convex price interpolation, confidence proxy and snapshot serialization.
- `mts_hvwap_candidate.py`: four optional fields and opt-in observation; shared immutable baseline enrichment preserves original voters and order authority.
- `mts_adaptive_ab.py`: paired decisions, locked mode-scoped daily JSONL collection, cost-inclusive win-rate/MDD/Sharpe comparison, five-weekday data gate, thresholds and CLI.
- `tmf_spread.py`: once-per-init config, original baseline verdict, paired observation at unchanged five-minute cadence, error isolation.
- `scripts/research/replay_adaptive_vwap.py`: complete-bucket CSV replay with explicit timestamp semantics, optional real events/outcomes, strict pair ID/timestamp joins and JSON/Markdown reports.
- `config/adaptive_vwap_config.yaml`, three focused test files, and `docs/ADAPTIVE_VWAP_INTEGRATION_GUIDE.md`.

## Evidence

| Check | Result |
| --- | --- |
| Pre-change focused integration/release baseline | 56 passed |
| Final focused algorithm, A/B, replay, prior integration and release tests | 144 passed |
| Fusion + A/B coverage, including branches | About 99%; exact artifact `/tmp/mts-adaptive-coverage.json` |
| Pure fusion performance, 1000 hot calls / 320 bars | Mean 0.412ms, p99 0.891ms, max 2.727ms |
| Both candidate observation arms, 100 hot calls / 320 bars per leg | Mean 4.607ms, p99 6.298ms, max 6.408ms |
| Existing trend/schema regression tests | 19 passed, same 2 previously evidenced failures |
| Broad suite with `--maxfail=1` | 30 passed, 1 previously evidenced config failure, then stopped; full suite not green/completed |
| Syntax | Five affected production Python files compiled |
| Whitespace | Full staged `git diff --cached --check` passed |
| Original OI / Vol / Quality modules | Unchanged SHA-256 identities |

Both hot-path performance checks meet the 10ms requirement for their measured scope. They exclude imports, upstream OI file scans, log writes and scheduling. The old 100k-event estimator benchmark remains an end-to-end runtime blocker; no claim is made that the full live tick pipeline now completes in 10ms.

The two targeted baseline failures remain `test_counter_trend_leg_mapping` and `test_hvwap_release_intent_keeps_gate_reason_out_of_mode`. The bounded broad run stopped on `test_unified_config_session_override_merging`, independently reproduced in the prior worktree during Task 1.3.2. None was altered or hidden by this task.

## Historical five-date replay

The user's supplement confirms that no existing paired A/B data exists. Its suggested October 7–11 dates are absent from the available June/July CSVs. The engineering replay instead used **2026-06-08 through 2026-06-12**, selecting timestamps from file content, not file names.

- Observed dates: June 8, 9, 10, 11 and 12.
- Complete paired candidate records: **251**.
- Adaptive observations with complete OI/quality inputs: **0**.
- Gaps: `OI_QUALITY_EVENTS_MISSING`, `PAIRED_NET_OUTCOMES_MISSING`, `ADAPTIVE_INPUTS_INCOMPLETE`.
- Result: **INSUFFICIENT_DATA / NO_GO**; actual win-rate, MDD and Sharpe comparison are null.
- Report: `/tmp/mts-adaptive-replay-20260608-12/reports/ab_comparison_2026-06-08_2026-06-12.md` (JSON alongside it).
- Paired logs: `/tmp/mts-adaptive-replay-20260608-12/logs/ab_test/`.

Synthetic fixtures separately verify numerical GO/NO_GO boundaries, weekday coverage, valid and foreign pair joins, missing calibrated segments, failed collectors, no volume influence on TWAP, as-of filtering, duplicate/gap handling and no changes to baseline arbitration. Fixture improvements are never presented as market evidence.

## Assumptions and remaining work

The integration guide records the specification conflicts and chosen interpretations: direction extremes are strong (correcting the printed inverted formula to match prose); TWAP weights completed intervals; quality comes from Task 1.3.2. Confidence is explicitly a quality/coverage proxy. Promotion uses the stricter +2 percentage-point win rate / 5% relative MDD reduction thresholds.

Phase 1 does not define or execute a new adaptive exit/fill policy. Logs distinguish candidate release eligibility and hypothetical marks from actual exits (`exit_executed=false`, `exit_price=null`). A real execution/replay model must produce paired net outcomes, including all fees/taxes/slippage. Verified dataset provenance, valid upstream event schemas, unresolved estimator performance and existing promotion gates remain prerequisites for Phase 2.

## Provenance and local artifacts

- deployment_id: `air4`; host_role: `offline_research`; instance: `mts-oi-vol-integration-20261015`.
- Hostname: `MacBook-Air-2.local`; ComputerName: `MacBook Air (4)`; architecture: `arm64`.
- Repository: `/Users/mylin/Documents/mylin102/tw-trading-unified-oi-vol-20261015`.
- Branch: `feat/mts-oi-vol-integration-20261015`; baseline: `1b9e904fa478977f305c2021f141f683061e234b`.
- Permitted mode: PAPER; operations: offline implementation/testing only.
- No `.deployment-target` or runtime/test artifacts are included in the source commit.
- Status before commit: MODIFIED_ON_AIR4; synchronization: NOT_SYNCHRONIZED; runtime: RUNTIME_NOT_VERIFIED.

Test/evidence files:

```text
/tmp/mts-adaptive-baseline.log
/tmp/mts-adaptive-validation.log
/tmp/mts-adaptive-coverage.json
/tmp/mts-adaptive-performance.json
/tmp/mts-adaptive-paired-performance-before.json
/tmp/mts-adaptive-paired-performance.json
/tmp/mts-adaptive-known-regressions.log
/tmp/mts-adaptive-full-attempt.log
/tmp/mts-adaptive-replay.log
```
