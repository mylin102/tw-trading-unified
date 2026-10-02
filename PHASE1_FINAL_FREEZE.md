# Futures PAPER selector — final freeze

2026-10-02 Codex. PAPER-only cutover, not LIVE promotion authorization.
Base: 2fc306d38fefc25376fd944c33f6acd335fabead.
Tested code HEAD: f4a858c2128cf25e0fdb16e1388a11a344efa054.
Application scope: ecosystem.config.js only. This commit is doc-only.
LRC_RELEASE_SHA must equal the literal final deployed release HEAD.

```text
frozen_tree_hash: a461e8e0eb9b96f12a610e4339ea9f2b2125bf24ece9d3200d63bff259c152fd
```

Standard re_freeze.py excludes this file, PHASE1_RC_CANDIDATE.md and
PHASE2_DEPLOYMENT_MANIFEST.md. No application edits after freeze.
Prior manifest backed up with SHA256 under:
/private/tmp/mts-paper-freeze-backup-codex-20261002/.

## Actual offline evidence

Real Node require(ecosystem.config.js) callsite: RED then four-case GREEN.
Default-live mutation failed; paper restored byte-identically to committed
ecosystem blob 97e35acffde12663e65e087dd698d4005b320430.
node --check and git diff --check passed.

Full A/B: Python 3.12.5, pytest 9.1.1, anyio 4.14.2; background taskpolicy;
isolated fake-broker runtime without live credentials. No concurrent pytest.
A: 149 failed, 3983 passed, 15 skipped, 10 collection errors.
B: 147 failed, 3985 passed, 15 skipped, 10 collection errors.
Both: 4147 collected/completed; 12447 actual phase/collection records.
New failed node/phase pairs and new collection errors: empty.
Added/removed/unfinished nodes: empty; duplicate collection: false.
Not an all-green suite. PM2 SSOT contract now passes. The other resolved
baseline node (reconnect-window current-generation BBO) is not credited here.

Raw artifacts:
/private/tmp/claude-501/-Users-mylin/e660ac12-1943-4be6-b6de-f2b0daef2a5f/scratchpad/
final_A_baseline.log, final_A_phase.jsonl, final_A_collected.txt,
paper_B1_codex_candidate.log, paper_B1_codex_phase.jsonl,
paper_B1_codex_collected.txt, paper_B1_codex_run.json,
paper_B1_codex_finished.json.
Comparison: /private/tmp/mts_compare_phase_codex_20261002.py.

## Mode and pending Mini acceptance

TRADING_FUTURES_MODE: default/paper -> futures; live -> futures_live;
invalid values reject. Preserve global/options PAPER_MODE=true.
Mode is startup-bound; no hot YAML mode switch or release-directory rotation.
No strategy/risk/YAML/Python/stock/options/gateway/deployment-identity changes.
Four pre-existing Python undefined names unchanged. No LIVE gate bypass.
Separate account-binding/gateway candidate is NOT included.

Mini acceptance PENDING: fresh broker FLAT/open_orders=0/health/identity;
single trading-only stop/start, old owner gone before Git source change;
exact SHA and clean tracked source; new paper_active context/hash/namespace;
paper OrderManager and near/far Tick+BidAsk acknowledgements;
new-process startup broker reconciliation and safety writer; 120s stable;
then existing maintenance-flag release and PM2 save/dump/launchd provenance.
Dashboard and stock stay untouched. No manual orders, ledger/context/safety
JSON edits, git stash, automatic second restart or LIVE authorization.

LIVE account-binding/margin problems remain outside this PAPER cutover.
Paper lacks LIVE certification session: preserve genuine pre-stop last-live
broker snapshot; do not relabel it a current paper-session broker query.

