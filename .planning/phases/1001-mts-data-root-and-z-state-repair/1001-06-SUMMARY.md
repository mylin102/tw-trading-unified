---
phase: 1001-mts-data-root-and-z-state-repair
plan: "06"
subsystem: verification-and-handoff
tags: [mts, offline, dashboard, maintenance-checklist]
requires: ["1001-01", "1001-02", "1001-03", "1001-04", "1001-05"]
provides: [offline source verification, explicit evidence limits, unexecuted maintenance boundary]
key-files:
  created: [.planning/phases/1001-mts-data-root-and-z-state-repair/1001-06-SUMMARY.md]
  modified: []
key-decisions:
  - Do not infer current Mini process, environment, schedule, or deployment SHA from offline source/docs.
  - Do not deploy or restart; production remains outside this phase's authority.
requirements-completed: [MTSZ-HANDOFF]
completed: 2026-10-08
status: handoff-with-production-unknowns
---

# Plan 1001-06: Offline handoff and verification

## Completion matrix

| Area | Result | Evidence / limitation |
|---|---|---|
| Monitor callback | Offline slice complete | `1001-02` and `/Users/mylin/Documents/MTS-plans/2026-10-06/evidence/gsd1001_monitor_candidate_20261008_comparison.json`; added callback tests pass. |
| State Z | Offline slice complete | `1001-03`; RED/FIX and mutation evidence in `state_red_20261008*` and `state_mutation_20261008*`; targeted state tests pass. |
| Loader | Offline slice complete; full A/B exception disclosed | `1001-04`; candidate 3 comparison has one changed flaky node, independently reproduced on both source sides. No claim of literal zero full-suite outcome differences. |
| Producer | Offline slice complete with targeted verification | `1001-05`; 31 affected tests pass. No full-suite rerun, as directed by user. |
| Static undefined-name gate | PASS for changed sources | Explicit checker on `core/spread_loader.py` and `scripts/update_calendar_spread.py`: 0 undefined names, 0 scan errors; 9 contract tests in combined targeted run passed (3 name-gate + 6 tree-gate). |
| Integration | PASS at source/fixture level | Fake producer `main()` published a formal CSV; real loader found it and returned the same final `spread_z`. Not a deployed integration test. |
| Dashboard source | Source contract aligned | Graph inspection: `ui.dashboard._all_spread_csvs` (line 3611) resolves `runtime_path("data")`, accepts only ticker/date formal CSV names; `load_calendar_spread_data` (line 3664) consumes these files. Producer now publishes to that same runtime data root and formal naming scheme. No dashboard process or page was accessed. |
| Current deployment identities | UNKNOWN | Only local `.deployment-target` was read: `air4`, `offline_research`, repo path matches this worktree. No remote/Mini process SHA or per-app identity was queried. |
| Cron payload / single owner | UNKNOWN | Historical repo example at `docs/rules/multi_host_synchronization.md:384` is not evidence of current scheduler state. No live schedule inspection. Atomic rename handles reader consistency, not producer ordering. |
| `TRADING_RUNTIME_DIR`, `NODE_ENV` on deployed apps | UNKNOWN | No production process environment was read. Isolated tests set `TRADING_RUNTIME_DIR` under `/private/tmp` and PAPER; this does not establish deployed values. |
| Backup/rollback/restart | NOT EXECUTED | No production artifact was changed, so no backup or rollback command was run. Restart necessity is unknown until the operator selects an authorized deployment target and validates its process model. |
| Production maintenance/deploy | NOT EXECUTED | Explicitly prohibited in this phase. No broker/API session, orders, ledger/runtime/context/profile write, PM2/cron/launchd change, restart or deploy. |

## Candidate and evidence identities

- Existing branch: `fix/mts-mode-data-isolation-20261002`.
- Final source candidate SHA: `4a8d1191c6d1bdb38563f00e5891280eae02697f`.
- Producer base SHA: `b31e010462c458332b444d5938130167e4747ced`; test-only RED `e4146b19`; source-only FIX `4a8d1191`.
- Prior phase commits in this chain: monitor supplement (see `1001-02`), state RED `54137b7a` / FIX `317f70ce` / telemetry-shape follow-up `edf94646`, loader RED `03b687d6` / test follow-up `568ff43d` / FIX `b31e0104`.
- Local app identities beyond this worktree and production originals/candidates are not claimed or guessed.

## Safe future maintenance checklist (not authorization to execute)

Before any later deployment, an operator must separately authorize it, then read the target host's own deployment identity and process definitions; capture exact current/candidate SHAs, scheduler command and environment; verify the runtime data root and single producer owner; preserve a target-host backup; and define a tested rollback and post-change observation window. This phase did not verify those host facts, so it supplies no executable production commands and does not recommend a restart/deploy now.

## No-side-effects declaration

All writes were limited to the existing Air4 offline worktree and test/evidence paths under `/private/tmp` or pytest temp. The pre-existing untracked `data/telemetry/renko_bricks/` and `data/telemetry/shadow_f/` remain untouched. No Mini access, broker session/query/order, runtime/ledger/context/profile update, deployment, restart, PM2/cron/launchd change, stash, prune, or production directory change occurred.
