---
phase: 1001-mts-data-root-and-z-state-repair
plan: "05"
subsystem: producer
tags: [mts, spread-csv, atomic-publish, runtime-root]
requires: ["1001-04"]
provides: [validated producer output, atomic runtime-root publish, producer-to-loader readback]
affects: ["1001-06"]
tech-stack: [python, pandas, pytest]
key-files:
  created: [tests/scripts/test_calendar_spread_atomic_publish.py]
  modified: [scripts/update_calendar_spread.py]
key-decisions:
  - Resolve the data destination through runtime_path("data") and validate output before creating/publishing a formal file.
  - Use a same-directory .tmp, flush/fsync, then os.replace; this guarantees readers see a complete old or new file, not concurrent-writer ordering.
  - Keep broker interaction fake; test the real producer main callsite and real SpreadLoader readback.
requirements-completed: [MTSZ-PUBLISH]
completed: 2026-10-08
status: complete-with-limited-scope-verification
---

# Plan 1001-05: Producer publish

## Implementation

- RED test-only commit: `e4146b19` (`test(producer): cover isolated atomic spread publish`). Base/source before fix: `b31e010462c458332b444d5938130167e4747ced`.
- FIX source-only commit: `4a8d1191c6d1bdb38563f00e5891280eae02697f` (`fix(producer): validate and atomically publish spread data`).
- Changed only `scripts/update_calendar_spread.py` and `tests/scripts/test_calendar_spread_atomic_publish.py` for this slice.
- The fake API drives `main()` through contract selection, kbar conversion, actual metrics calculation and publication. It verifies the actual loader can read the published file and return matching `spread_z`.
- Validation retains the 20-sample rolling behavior, 19-row warmup NaNs, and undefined Z for zero standard deviation. It rejects non-finite quotes/spread, bad timestamps, malformed schema, and inconsistent derived spread before publication.
- Temp file is unique, same-directory, `.tmp` (not a formal loader filename), flushed/fsynced and atomically replaced. Failure cleans only its own temp file and preserves any prior formal file hash.

## Verification (targeted per user's instruction)

RED command ran against base SHA `b31e0104`: `pytest tests/scripts/test_calendar_spread_atomic_publish.py -vv --tb=short`; result **4 failed, 2 passed**. Failures exposed incorrect data-root resolution, invalid input replacing the formal CSV, and replace failure handling.

After FIX, command:

`AGENT_NAME=codex TRADING_RUNTIME_DIR=/private/tmp/mts-producer-focused-20261008 python3 -m pytest tests/scripts/test_calendar_spread_atomic_publish.py tests/core/test_spread_loader_shared_runtime.py tests/core/test_spread_loader.py tests/contracts/test_undefined_name_gate.py tests/contracts/test_undefined_tree_gate.py -q`

Result: **31 passed**. Static checker on both changed source files: `0 undefined names; 0 scan errors`. `git diff --check` passed before commit.

Mutation probe temporarily disabled `validate_spread_output(df_spread)`: **2 failed, 4 passed**. Both failures were the bad-input tests, which then incorrectly published. The production source was restored and the 31-test targeted run passed afterward. No full-suite rerun was done, per user's request to run only changed/affected tests.

## A/B, overlap and limitations

- Slice-level RED→FIX evidence is against exact base `b31e0104`; changed-source producer regressions and directly affected loader/static-gate tests pass.
- No new full-suite candidate was run for this producer slice because the user explicitly requested not to spend time rerunning unaffected tests. Therefore full-suite zero-new-failure is **not claimed**.
- Existing loader full candidate evidence is preserved in `/Users/mylin/Documents/MTS-plans/2026-10-06/evidence/gsd1001_loader_candidate3_20261008_comparison.json`. That broader comparison has one changed result (`test_real_order_export_and_restore_are_mode_scoped`); separate base/candidate replay proved the same assertion fails on both sides, but the full candidate still does not satisfy a literal zero-outcome-change gate. This producer slice does not modify that test or its code path.
- Atomic replacement protects concurrent readers; it does not serialize concurrent producer writers. Repo documentation contains a historical cron example (`docs/rules/multi_host_synchronization.md:384`), not proof of the currently installed schedule or overlap. Current schedule and host ownership are **unknown/unverified**; no cron/PM2/launchd state was inspected or modified.

## No side effects

Fake API only; no Shioaji session or credential. Tests used temporary data under pytest temp and `/private/tmp`. No broker, production runtime, ledger/context/profile, PM2, cron, restart, deploy, stash, prune, or existing telemetry changes. The two pre-existing untracked telemetry directories remain untouched.
