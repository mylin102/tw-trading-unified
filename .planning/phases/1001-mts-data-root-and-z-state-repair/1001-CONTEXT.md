# Phase 1001 Context — AGENT_NAME=codex, 2026-10-06
## Locked decisions
- User approved a separate GSD phase and execution of the existing four-source offline repair.
- Authority: /Users/mylin/Downloads/MTS_AGENT_EXECUTION_INSTRUCTIONS_REVIEWED_20261006-2.md.
- Detailed execution contract: /Users/mylin/Documents/MTS-plans/2026-10-06/EXECUTION_PLAN.md.
- Preserve existing worktree, branch, commits and untracked telemetry. No new production directories.
- One implementer, sequential slices; source/test maximum two files per slice.
- Separate test-only RED and fix-only commits. Actual callsite tests; mutation restoration proof.
- Full per-slice A/B with actual node/phase comparisons. Conditional base evidence reuse only with proven identical conditions.
- Keep 20 one-minute rolling sample-standard-deviation calculations and existing trading decisions.
- Warmup NaN and zero-standard-deviation undefined Z are not entire-file corruption.
- PAPER needs no broker position confirmation. No real broker session on Air4.
## Prohibitions
No production deploy/restart, cron/PM2/launchd/environment change, runtime/ledger/context/profile writes, symlinks, directory moves, stash or prune. No fifth source change.
## Goal boundaries
Offline fixes, evidence and a reviewable maintenance checklist are the phase goal. Production source/data consistency remains unverified until separately authorized maintenance.
## Evidence at registration
HEAD e2f196bd; historical test commits 62fbb856 and 281be448. Full A/B comparison has complete 4224/4230 nodes, unchanged 111 failing node/phases and 10 collection errors, six added regression nodes, no removed nodes. Supplementary delayed-fill coverage remains outstanding.

