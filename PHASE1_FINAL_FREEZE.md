# MTS PAPER confirmed peak restart recovery — 2026-10-13

## Current deployment completion — 2026-10-13

2026-10-13 Codex; AGENT_NAME=codex. The PAPER-only, engine-only Mini release
was completed and recorded as VERIFIED in
`/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime/logs/deployments/codex-4a9591f64ccf4dbca9b814e19558be40/complete.json`.
Running `trading-system` is pinned to release18 SHA
`ec47a54df528632257b0ea7c5edd06f90a339855`; dashboard remains at
`45f0ced7c7ea5317c610677c323262f15df7a7a2`; stock-runner was not restarted.
The PAPER profile hash is `4e3c871137587205aab9a7685f31bcd4156bb9a1e2ca39bb94201e70b1044c4a`.
PM2 save completed; all 24 samples passed across 124.95 seconds while the
futures session was open. This transaction's ENTRY lock is absent. Final PM2
status: trading-system PID 86087 online, dashboard PID 49778 online, and
stock-runner PID 41421 online.

The start-stage PM2 command initially failed after the prior engine stopped.
The canonical PAPER engine was restored under the retained entry lock, then
the controller continuation did verify/save/unlock only; it did not stop or
restart that recovered process. Two subsequent attempts correctly failed
closed and retained the lock: first the startup marker fell outside a 512 KB
log tail, then PM2 rotated its startup segment out of the active log. Test-first
regressions cover long logs and startup logs rotated before/during observation.
Mutations restoring tail-only/no-archive reading or re-reading after rotation
fail the regression; the restored controller suite is 30 PASS. Controller
commits: `68ea2c2c` test → `c0eb7b02` implementation; long-log tests
`8d8c1e3a` / `ffc2f01e` → `6cba1153`; rotation tests `bf42df9b` /
`b4db8ea1` / `cd54ac8c` → fixes `ad148fa3` / `272cc182`. The managed-root
receipt correction is `1f0839cd` test → `b5cb261f` fix.

No broker query, order, manual state/ledger change, LIVE enablement, dashboard
restart, or stock-runner restart. Normal PAPER runtime writes and market-data
activity occurred. The completion receipt records zero broker queries, zero
manual orders, and no manual state/ledger edits. Six temporary controller
worktrees created for this operation were removed after completion; unrelated
worktrees and user telemetry/data were left untouched. This supersedes the
earlier pending-deployment notes in the 2026-10-13 sections below.

## Current follow-up: protective ATR risk release + retained PAPER deploy lock — 2026-10-13

2026-10-13 Codex; AGENT_NAME=codex. User authorized fixing the missing
protective release path and deploying. Candidate includes test `40e92d51`
and fix `7add2c0f`: an ATR-triggered protective release no longer requires
the PAPER trend snapshot's `pass_release`; quote validity/width, lifecycle,
price and order/idempotency guards remain. Discrete TREND_RELEASE continues
to require its trend snapshot. The field remains for compatibility/telemetry.
The real-callsite regression was RED against the prior implementation and
GREEN after the fix. The nearby focused strategy run is 39 PASS / 3 existing
failures: one unrelated counter-trend side-mapping expectation and two
integration tests lacking TMF calendar-spread CSV fixtures. It is not a
full-suite claim.

The previous deployment attempt `codex-ec7ef959b63d4ba5ab1a2a7c590106b4`
failed at observe and intentionally retained its PAPER entry lock. Deployment
controller test `17cab394` was RED before implementation; `286a508d` added
strict adoption of only that exact matching failed-observe lock. The Mini
receipt exposed an additional format fact: its `pm2` map contains all apps,
while `heads` contains only deployment roots. Regression `1f0839cd` reproduced
that mismatch RED; fix `b5cb261f` validates exactly the two deployment roots
and their prior PID/cwd/SHA/status while allowing unrelated app snapshots.
The first live preflight rejected the mismatched validator before lock
adoption, PM2 mutation, checkout or unlock; the original token remains. Final
controller suite at this milestone: 29 passed. This does not modify trading
state/ledger or query the broker. The later completion and additional
controller verification fixes are documented at the top. Deploy engine-only
to release18; keep
dashboard SHA `45f0ced7c7ea5317c610677c323262f15df7a7a2` and stock-runner
untouched. A failed resumed attempt must retain its lock; never manually
remove it or rewrite state. PAPER only; no LIVE promotion.

No broker, order, runtime state, ledger, PM2 process, stash, dashboard, or
stock-runner was changed during offline implementation/testing. At this point
the formal Mini deployment was pending; it was later completed as recorded at
the top of this file.

## Follow-up: HVWAP unsent PAPER release retry — 2026-10-13

2026-10-13 Codex; AGENT_NAME=codex. User said go after reviewing today's
PAPER trade: repair the stranded single-leg release after bad BBO. Test-only
commits faf4e344 and 136a819a; fix fcb11e69982c91058977451a54e1c498dfdbf599.
This freeze manifest binds that source for an engine-only PAPER release18
candidate; dashboard SHA remains 45f0ced7c7ea5317c610677c323262f15df7a7a2.
frozen_tree_hash: 1213d6de1dc43432d3e24734bc3fe914966d5d047b62cab538f0752b9a8d540d

Confirmed incident: trade mts-auto-091504-132 emitted HVWAP_RELEASE_INTENT
for NEAR at 09:28:33.722368, then actual dispatcher emitted
ORDER_INTENT_BLOCKED/PAPER_EXIT_BBO_INVALID_OR_STALE at .734466 before any
order submission. Later 120 ALIGNED_PASS records followed, all with
release_action_emitted=false. The strategy set its per-trade sent latch and
release timer at emission. The monitor's proven-unsent recovery reset the
generic lifecycle but omitted these two HVWAP fields.

Fix only releases the matching trade's HVWAP latch/timer after the existing
pending-order and durable-intent checks establish the PAPER exit was not
submitted. Pending lifecycle orders and durable SUBMIT_ATTEMPTED intents do
not enter the reset branch. No quote gate, strategy criterion, LIVE path or
fill semantics changed.

Focused A/B across quote-retry, executable-BBO and exit-width tests: mutated
pre-fix base 14 PASS/1 FAIL; candidate 15 PASS. The only changed node is the
new same-trade HVWAP retry regression; no removed or added-unmatched nodes.
Mutation retaining both old latch and timer: 14 PASS/1 FAIL, same regression;
restored code then 15 PASS. Initial RED before fix: new regression failed at
retry emission. Raw evidence:
/private/tmp/mts_hvwap_retry_mutated_20261013.xml and
/private/tmp/mts_hvwap_retry_candidate_20261013.xml. Not a full-suite claim,
per scoped-test preference.

Implementation is offline on Air4, branch fix/mts-mode-data-isolation-20261002.
Only monitor.py changed for fix; the regression extends the existing quote
retry test module. Mini restart is a separate maintenance operation; no
deployment has yet been performed for fcb11e69. PAPER only, no broker account
query, no manual order, no state/ledger/context edits, no LIVE activation,
no stash, no dashboard or stock-runner edits.

The October 12 and older sections below are archival evidence.

2026-10-13 Codex; AGENT_NAME=codex. User explicitly requested Mini to run
the new version. PAPER maintenance deployment, trading-system only in
release18; dashboard stays 45f0ced7c7ea5317c610677c323262f15df7a7a2.
Test-only commits 418152ae, e711d23a, 13a79eb7 precede tested fix
5efab57dd38a630a39c81b9404033d63956ef3f1. This commit changes only this
excluded freeze manifest, not application code.

Recover only same-trade confirmed PAPER Policy J durable peaks from existing
PAPER events after entry quarantine. LIVE, unknown mode, new-entry quarantine,
foreign trades, candidates, nonfinite peaks and released-leg records are not
recovery evidence. First synchronized baseline still skips giveback evaluation;
invalid/stale BBO remains blocked. No manual close or fabricated fill.

Focused A/B: baseline 110 PASS/1 existing SKIP, candidate 125 PASS/1 SKIP.
All 111 baseline node outcomes identical, 15 added nodes PASS; zero new
failures, missing nodes or changed outcomes. Not a full-suite claim, per user
instruction. Recovery-disabled mutation fails all 3 positive real-callsite
tests, including actual JSON/fill-ledger restore followed by on_bar and
invalid-quote dispatch/retry. Mutations restored before final candidate run.
Raw /private/tmp/mts_pj_restart_{base,candidate,mutation}_20261013.log;
base/candidate JUnit XML at the same prefix. Candidate completed in 5.15s.

Only engine maintenance deployment is authorized. Preserve local PAPER
position/ledger/history and all unrelated PM2 owners; no stash, manual
orders, broker account queries, LIVE enablement or state/context rewriting.
Normal engine writes and market-data connection after restart are expected.
Verify exact SHA, canonical cwd/interpreter, local position/ledger agreement,
PAPER mode/hash, four quote subscriptions, dashboard classifier/HTTP and
120s stable observation before save/unlock. Closed-session freshness is
exempt only during canonical closed hours. Opening alone is not proof of
an exit fill; observe fresh-quote strategy execution after opening separately.

The October 12 and older sections below are archival evidence.

## Archived MTS PAPER exit recovery + engine-only deployment — 2026-10-12

2026-10-12 Codex; AGENT_NAME=codex. Current approval is PAPER maintenance
deployment to Mini release18 only. Tested application candidate 59806d6d;
engine-only deployment RED 65bc6522, fix 6327d933. This manifest-only commit
does not change application code. The exact final commit must bind PM2 SHA.

Exit recovery A/B: base 112 PASS/1 existing SKIP, candidate 132 PASS/1 SKIP;
all 113 baseline outcomes identical, 20 added PASS. Deployment A/B: base
19 PASS, candidate 27 PASS; all baseline outcomes identical, 8 added PASS.
Mutation of engine-only stop isolation fails its real CLI regression; restored.
Raw evidence: /private/tmp/mts_engine_only_{red,green,mutation,restored}_20261012.log
and /Users/mylin/Documents/MTS-plans/2026-10-06/MTS_EXIT_RECOVERY_EVIDENCE_CODEX_20261012.md.
Only affected tests run per user instruction; not a full-suite assertion.

Deploy with --engine-only; dashboard SHA stays 45f0ced7c7ea5317c610677c323262f15df7a7a2.
Preserve dashboard PID/root/HEAD and existing generated CSV differences;
reject source edits, staged/deleted data, unknown drift. No stash or cleanup.
Preserve PAPER ledger and existing spread via normal local restore, no forced
flatten/manual orders/state/context edits, no broker account query, no LIVE.
Exact commit/identity/config hash, single engine, four subscriptions, dashboard
classifier and HTTP, fresh market-open state, ledger agreement and 120s stable
ENTRY-locked observation are required before save/unlock. Other PM2 owners
must remain unchanged. No cron or credential changes authorized.

All sections below are archival; the current October 12 scope takes precedence.

## Archived PAPER close/startup consistency — final freeze 2026-10-06

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
frozen_tree_hash: 1213d6de1dc43432d3e24734bc3fe914966d5d047b62cab538f0752b9a8d540d
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

## Runtime certification metadata
## Runtime certification metadata
<!-- BEGIN RUNTIME_CERTIFICATION_METADATA -->
```json
{
  "broker": {
    "active_orders": 0,
    "capture": "OK",
    "futures_positions": 0,
    "mode": "live",
    "source": "live_broker"
  },
  "callback_registration_generation": null,
  "candidate_head": "4ac011f8f09818071a33bb1525e7edea4da8f1c6",
  "captured_at": 1790905674995,
  "clean_worktree": true,
  "config_hash": null,
  "durable_locks": {
    "active": 0,
    "retired_unresolved": 7
  },
  "generated_at": "2026-10-20T03:25:24.268735+00:00",
  "live_order_allowed": false,
  "process": {
    "pid": null,
    "start_identity": null
  },
  "promotion_mode": "paper_active",
  "refresh_generation": "futures-1790905675114377000-11506",
  "session_id": "5efc6b91b43adb42075104e6d180fc55",
  "source_tree_hash": "1213d6de1dc43432d3e24734bc3fe914966d5d047b62cab538f0752b9a8d540d",
  "ttl_seconds": 600.0
}
```
<!-- END RUNTIME_CERTIFICATION_METADATA -->

