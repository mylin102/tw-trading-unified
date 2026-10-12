# PAPER close/startup consistency — final freeze 2026-10-06

2026-10-06 Codex; AGENT_NAME=codex. User authorized verified repair then direct
PAPER deployment, and closed-session heartbeat bypass. Tested code:
b992e5d52b029354259b5e39b78870188669b350. Freeze-only manifest commit; no
application edits after the full run. Canonical engine stays release18.

Separate test -> fix commits:
callback race 32ceefaf -> 580c4c76;
close projection 6b98858e -> b16f2caa;
explicit PAPER row scope 40438e4e -> 9c1b50a4;
actual tick/reset/writer 2d1cafd0 -> f634dfc6;
actual monitor.run local startup 42fe4daf -> b992e5d5.
Every slice <=2 source/test files; real callsites, RED and separate commits.
Mutations respectively 1,2,1,2,6/4 failures, all committed bytes restored.
Final focused startup GREEN 30 PASS; tick/LIVE-adjacent GREEN 53 PASS.

Complete actual-node A/B c9e848dc -> b992e5d5: 4200 -> 4224 collected AND
finished, 4074 -> 4098 PASS, unchanged 111 failed-node/phases, 15 SKIP and
10 collection errors. All 24 added nodes PASS; no new failures/errors,
removed/unfinished/duplicate nodes. This is NOT an all-green suite.
Each intermediate full A/B also has zero new failures/errors. Raw logs,
actual node/phase lists, mutations and exact-SHA comparisons:
/private/tmp/mts-modeisolation-evidence-codex-20261002/
paper_consistency_final_comparison_20261006.json.

Acceptance: exact frozen SHA; stop old engine before ff-only source update;
one canonical PAPER engine, PAPER_ACTIVE/live_order_allowed=false, unchanged
config hash and PAPER namespace. No broker-flat/holdings/reconciliation query
needed for PAPER. Startup empty bar invokes normal local reset/writer, never
an entry signal; LIVE/unknown accounting/recovery protections unchanged.
Local same-trade readable dual-entry/close proof; no manual fill/state/context
edits, no history rewrite, no stash, no manual orders/cancels, no LIVE enablement.
Protect current dashboard and stock owner snapshots; no restart/source edits
to either. Near/far Tick+BidAsk acknowledgements, genuine cleared local state
and zero UPL, 120s stable ENTRY-locked observation, then unlock and PM2 save/
dump/launchd consistency. Heartbeat checks bypass ONLY during canonical
market-closed hours; process/SHA/mode/state/HTTP checks remain mandatory.

The October 5 sections below are archived evidence, NOT current deployment scope.

## Archived dashboard near-leg PnL freeze 2026-10-05

2026-10-05 Codex: user authorized dashboard-only deployment to Mini.
Test-only commit c309653c689cfbde085e65fc2514635099306a34;
tested fix 2fc842cace1a6f1618e6ab9f00b0e525b3edddd7.
This freeze-only commit changes no application code. Deploy to an isolated
dashboard release; trading-system stays on df4fcf5c and stock-runner stays
on its current release. Do not restart either trading owner, change mode,
write ledgers/context/positions, query broker, or create ENTRY locks.

Actual dashboard callsite RED: 7 failed/6 passed; focused GREEN: 30 passed.
Last-fill-only mutation: 7 failed/6 passed, exact committed bytes restored.
Complete actual-node A/B df4fcf5c -> 2fc842ca:
4063 -> 4073 passed; 112 failures, 15 skipped, 10 collection errors unchanged;
4190 -> 4200 collected AND completed. Zero new failed node/phases or
collection errors; all 10 added nodes pass, no removed/unfinished/duplicate
nodes. This is NOT an all-green suite. Evidence:
/private/tmp/mts-modeisolation-evidence-codex-20261002/
leg_pnl_candidate_20261005_comparison.json.

Acceptance: SHA-pinned dashboard-only PM2 owner, frozen clean tracked tree,
HTTP health and rendered October 5 rows near 0/-1370/-350, far 80/1000/120,
total 80/-370/-230 unchanged; 120 seconds stable with protected trading and
stock owners unchanged, then PM2 save/dump and launchd-home verification.
PAPER remains paper_active with live_order_allowed=false. Runtime account
values are not synthesized, and no broker-flat proof is required for a UI
restart. Existing gross-vs-net labels are deferred outside this slice.

## Archived morning October 5 freeze

2026-10-05 Codex: user authorized controlled PAPER deployment of tested
code 04320bade9ab612b6f5589681c24b9084ac6292f. This freeze-only commit
changes no application code. LIVE stays disabled, no broker-flat query
required for PAPER, no manual order/fill/ledger/context repair.

Fill slice: 6e04fd8a/3d2b51b5 tests → 7f124ef7 fix. Actual production
submit/gateway/manager/simulator regression RED 5 failures, GREEN 12 passed;
mutations broker gate 4 failures and local identity 5 failures, restored.
Complete A/B 013f4b9f → 7f124ef7: 147 → 111 failed, 4016 → 4061 passed,
15 skipped and 10 collection errors unchanged; 4178 → 4187 collected AND
completed; no new failure phases/errors, removed/unfinished/duplicate nodes.

UI slice: 5936091c tests → 04320bad fix. Actual dashboard FLAT UPL AST
branch RED 1 failure, final UI+fill focused 15 passed; mutation 1 failure,
restored. Complete A/B 7f124ef7 → 04320bad: 111 failures unchanged,
4061 → 4064 passed, 15 skipped/10 collection errors unchanged; 4187 → 4190
collected AND completed; no new failure phases/errors or removed/unfinished/
duplicate nodes. The suite is NOT all green. Exact complete comparison
artifacts paper_combo_candidate_20261005_comparison.json and
paper_upl_candidate_20261005_comparison.json are under Air4
/private/tmp/mts-modeisolation-evidence-codex-20261002/.

PAPER simulator fills have explicit PAPER-local ordinal IDs; LIVE receipt
identity and broker confirmation requirements remain. PAPER UPL no longer
falls back to stale LIVE snapshots; insufficient paper evidence remains N/A.
No strategy/risk/config changes, no retroactive fill for today's missed order.

Acceptance: same release18 and exact Git SHA; preserve untracked data and
all history; single-instance stop/start via ecosystem.config.js, near/far
Tick+BidAsk acknowledgements and fresh local PAPER heartbeat; 120 seconds
stable ENTRY-locked observation, then unlock ENTRY and PM2 save. Only
trading-system/dashboard targeted; stock-runner must remain unchanged.

The October 4/2 sections below are archived evidence, not current sign-off.

## Archived October 4 freeze

2026-10-04 Codex. PAPER-only MTS restoration, not LIVE promotion authorization.
Test base: bd095cde085ab661b361384f24f0d391c92c841d.
Tested code HEAD: ae85bbe9c429e0bfeda9632349f47d668efc0cc3.
Current repair scope: main.py and tests/core/test_startup_recovery_gate.py.
Includes previously verified mode-data isolation, PAPER config identity,
extrema field and reconnect-fixture corrections in the ancestor chain.
This final freeze commit changes only this excluded manifest.
LRC_RELEASE_SHA must equal the literal final deployed release HEAD.

User explicitly authorized MTS PAPER deployment and confirmed PAPER does
not require broker position confirmation. Startup and periodic account
reconciliation are LIVE-only when all profiles explicitly select PAPER;
LIVE/mixed/uncertain profiles retain the original fail-closed behavior.
Quote freshness and all LIVE certificate/order chokepoints are unchanged.

Current complete actual-node A/B: A=148 failed,4002 passed,15 skipped,
10 collection errors; B=148 failed,4015 passed,15 skipped,10 collection
errors. 4165 -> 4178 collected/completed; 13 added nodes all passed.
New failed node/phases and collection errors are empty; no removed,
unfinished or duplicate nodes. This is NOT an all-green suite.
Comparison: /private/tmp/mts-modeisolation-evidence-codex-20261002/
paper_account_AB_comparison_20261004.json.
Focused restored: 17 passed, no skips. Startup mutations: 2 and 8
failures; periodic mutation: 2 failures; exact committed code restored.

Mini acceptance pending: canonical release18, exact Git SHA/clean tracked
source, preserved old config/history, existing owner proven PAPER with
LIVE orders disabled, near/far Tick+BidAsk acknowledgements, separate
PAPER state namespace/config hash, exactly one engine and 120s stable
ENTRY-locked observation, then PM2 save/dump/launchd-home consistency.
Broker-flat proof and LIVE margin/session certification are not PAPER
acceptance criteria. Never forge broker-flat evidence or enable LIVE.
Only trading-system and dashboard may restart; stock-runner is untouched.

The remaining sections archive the earlier October 2 selector slice.
Their scope, counts and broker-flat requirements are historical and
superseded for this PAPER restoration by the evidence and authority above.

```text
frozen_tree_hash: 57de600d399d372d7dae9243149f16921a4496456506ac89c2fbb1be8655873e
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
