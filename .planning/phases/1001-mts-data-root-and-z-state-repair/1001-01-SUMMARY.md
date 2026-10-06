---
phase: 1001-mts-data-root-and-z-state-repair
plan: "01"
subsystem: testing
tags: [mts, paper, entry-z, regression-evidence]
requires: []
provides:
  - Actual historical monitor A/B node/phase and special-outcome comparison
  - Fresh complete baseline with explicit night-session test discrepancy
affects: ["1001-02", "1001-03", "1001-04", "1001-05"]
tech-stack:
  added: []
  patterns: [isolated-runtime, fake-broker, actual-node-phase-comparison]
key-files:
  created: [.planning/phases/1001-mts-data-root-and-z-state-repair/1001-01-SUMMARY.md]
  modified: []
key-decisions:
  - Preserve historical RED/FIX commits; append delayed-fill coverage separately.
  - Do not mislabel calendar-date test failure as a source regression or all-green run.
requirements-completed: [MTSZ-AB]
coverage:
  - id: D1
    description: Historical monitor common nodes have zero new failures or special-status differences.
    requirement: MTSZ-AB
    verification:
      - kind: other
        ref: /private/tmp/mts-modeisolation-evidence-codex-20261002/zsync_candidate_20261006_comparison.json
        status: pass
      - kind: other
        ref: python3 /private/tmp/mts_outcome_diff_codex_20261006.py zsync_base_20261006 zsync_candidate_20261006
        status: pass
    human_judgment: false
duration: 80min
completed: 2026-10-06
status: complete
---

# Phase 1001 Plan 01: Existing monitor A/B evidence

Historical monitor A/B has identical common phase outcomes, six added PASS regressions, and no changed collection/skip/xfail sets.

## Performance

- Execution identity: AGENT_NAME=codex; inline GSD execution, no dispatched agent.
- Tasks: 1/1 evidence closure; source/test files modified: 0.
- Fresh baseline full-suite elapsed: 996.75 seconds.

## Accomplishments

Historical base `a26c52baf932bebfc5bf441a2d18d2023f334b58` to fix `e2f196bd3442189ad6b9f21c61651981e7b72cf9`:

| Evidence | Base | Candidate | Difference |
| --- | --- | --- | --- |
| Collected / finished nodes | 4224 / 4224 | 4230 / 4230 | Six added regression nodes, all PASS |
| Failing node/phases | 111 | 111 | Exact same set |
| Collection errors | 10 | 10 | Exact same set |
| Skipped nodes | 15 | 15 | Exact same set |
| XFAIL / XPASS verbose statuses | 0 / 0 | 0 / 0 | None |
| Common phase outcome changes / removed nodes | — | — | None |

Both full commands: `/usr/bin/caffeinate -i /usr/sbin/taskpolicy -c background /Users/mylin/Documents/mylin102/tw-trading-unified-release15/.venv/bin/python3 -m pytest -p claude_phase_plugin tests/ -v --continue-on-collection-errors`.

All evidence is under `/private/tmp/mts-modeisolation-evidence-codex-20261002`: labels `zsync_base_20261006`, `zsync_candidate_20261006`, each `.log`, `_phase.jsonl`, `_collected.txt`, `_run.json`, `_finished.json`, plus candidate `_comparison.json`. Supplementary outcome tool: `/private/tmp/mts_outcome_diff_codex_20261006.py`; it reads original logs without rewriting them. Legacy phase plugin lacks `wasxfail`, so verbose raw statuses are used for that distinction.

Historical targeted/RED/mutation logs: `/Users/mylin/MTS_Z_SYNC_RED_CODEX_20261006.log`, `/Users/mylin/MTS_Z_SYNC_FOCUSED_CODEX_20261006.log`, `/Users/mylin/MTS_Z_SYNC_MUTATION_CODEX_20261006.log`. Test commits `62fbb856`, `281be448`; fix-only commit `e2f196bd`. Mutation restoration monitor SHA256: `2f17c4a0f8d2bb426e43b6dea7b6ebded086c91a9aed3f193eb2202c58d8357d`.

## Fresh baseline and evidence limitations

Fresh `gsd1001_base_20261006` at docs-only `5510f58e2c3ec51154544d0a81a0700bfa9ba3e1`: 4230 collected/finished; 4103 PASS, 112 FAIL, 15 SKIP, 10 collection errors. No source/test differences from `e2f196bd` (only ten GSD documents).

Comparison with historical candidate is **not** zero-new-failure: the additional call-phase failure is `tests/core/test_mts_mode_data_isolation.py::test_real_order_export_and_restore_are_mode_scoped`. Its assertion expects calendar date `TMF_20261006_orders.json`; actual night-session writer uses `get_session_date_str()` and exports `TMF_20261007_orders.json`. Fresh targeted triage label `gsd1001_clock_triage_20261006` reproduces this (one PASS, one FAIL). Actual fixture file `/private/var/folders/lp/x1m0y1fj1nv338sh033943w40000gn/T/pytest-of-mylin/pytest-193/test_real_order_export_and_res0/runtime/paper/exports/trades/TMF_20261007_orders.json` contains only `PAPER-OWN`. This is not a missing export or evidence that this Z fix changed source behavior. Preserve it as a baseline discrepancy; do not edit the out-of-scope test or date convention.

Historical manifests prove interpreter, command, separate isolated runtime and no concurrent MTS suite. They do not contain frozen package/config/fixture fingerprints; do not claim strict conditional next-slice baseline reuse from those manifests. Fresh full baseline was run instead. GSD metadata commits may require another fresh baseline under the user's exact-SHA rule; any relaxation requires explicit user consent.

## Task Commits

- Historical RED/test precision/fix retained: `62fbb856` → `281be448` → `e2f196bd`.
- GSD registration: `5510f58e` (docs only).
- This plan adds evidence summary metadata only; no new fix commit.

## Deviations from Plan

- User mandates existing `fix/mts-mode-data-isolation-20261002` worktree/branch; no GSD agent-* branch is fabricated.
- User's explicit zero-new-failure rule permits existing failures; GSD generic all-green language is not used to conceal them.

## Issues Encountered

Night-session calendar-date mismatch described above is deferred, not fixed. Fresh baseline and historical candidate cannot be advertised as identical outcomes.

## Next Phase Readiness

Ready for the monitor test-only delayed/duplicate-fill supplement. It remains outstanding; strategy state, loader, producer and maintenance checklist are not complete.

## No side effects

No Mini access, broker session/query/order, deployment/restart, PM2/cron/launchd change, production runtime/ledger/context/profile edit, symlink, stash, prune or production directory move. Only Air4 GSD metadata and out-of-tree test/evidence artifacts were written. Existing untracked telemetry is untouched.

## Self-Check: PASSED

Actual historical node/phase comparison and supplementary special-status comparison pass. Fresh full baseline completes with the explicitly recorded additional time-sensitive failure, not hidden or signed off as zero-new-failure.
