# R-008: Release-Suppression Counterfactual via Policy J Forward Replay — SPEC

**Status:** SPEC (not yet implemented)
**Start Date:** 2026-11-19
**Evidence Level:** target E3 (Counterfactual Evaluation), contingent on §5 validation gate
**References:** ADR-006, ADR-027 (CLOSED), R-002, R-003, R-005, R-007

---

## 1. Problem

Decision pending from user: should the RELEASE (split-leg) mechanism in
`tmf_spread.py` be disabled, keeping Policy J `COMBINED_EXIT` as the only
exit path? (A `release_enabled` kill switch now exists — commit `66652c08`,
deployed to Mini `602203cb`, default `True` = no behavior change — but
whether to ever flip it to `False` has no evidence behind it yet.)

Two prior attempts in this research line answered the WRONG question:

| Attempt | What it actually measured | Why it's wrong |
|---|---|---|
| `_record_release_counterfactual()` (`tmf_spread.py`, event `RELEASE_COUNTERFACTUAL`) | PnL of an **immediate forced combined exit** at the exact release-decision bar, using current bid/ask | Nobody runs this strategy variant — "not releasing" means **continuing to hold both legs**, not instantly flattening |
| This session's first attempt (retracted) comparing 39 `RELEASE_COUNTERFACTUAL` records to real outcomes | Same flaw, inherited from the field above | Aggregate delta (+20.3 TWD / -10,957.8 TWD total) answers "instant-exit vs release", not "hold-and-wait vs release" |

**Correct counterfactual:** for each historical RELEASE decision, replay
forward holding BOTH legs (as if release never fired) until Policy J's
**real** `COMBINED_EXIT` trigger condition would have fired on its own, and
compare PnL at THAT point to the real (released) outcome. If Policy J would
never have fired before session end, the counterfactual terminal point is
session close (or the existing ADR-027 evidence on SINGLE_LEG hold times, if
relevant as an upper bound).

---

## 2. Why this requires a NEW replay engine (not reuse of R-002/R-003)

- R-002 (`evaluate_lifecycle_actions()` harness) proved the RELEASE decision
  itself is reproducible from reconstructed `LifecycleContext` — useful
  infrastructure, but it answers "would RELEASE fire again", not "what would
  Policy J's COMBINED_EXIT have done afterward if RELEASE never fired".
- R-003 tested sensitivity of the RELEASE decision to `release_stop_threshold`
  — same scope limitation as R-002, single-bar decision, not a forward
  economic replay.
- Policy J's actual trigger is **path-dependent**, not a static threshold:
  `combined_upl_activation_net_pnl_twd` (default 300) arms tracking, then
  `_update_policy_j_peak()` tracks a **peak + giveback** state machine with
  its own guard/quarantine/freshness branches (`POLICY_J_PEAK_CONFIRMED`,
  `POLICY_J_PEAK_REJECTED`, `MARK_NOT_FRESH`, `POLICY_J_TRIGGER_SUPPRESSED`,
  `confirm_ticks`/`confirm_ms` debounce). A static-threshold replay would
  silently reproduce the exact mistake already made and retracted twice this
  session.

---

## 3. Data availability (confirmed 2026-11-19)

- `tw-trading-unified-release21/logs/raw_ticks/{TMFL6,TMFA7}_<date>_ticks.csv`
  — per-day, per-contract tick series for both near (TMFL6-pattern) and far
  (TMFA7-pattern) legs. Covers the window needed for recent RELEASE events.
- NOTE (separate, not-fixed-here issue): these paths are **cwd-relative**
  under the release tree, not routed through `mts_runtime_path()` — same
  class of bug as the two already-fixed in this session (`order_manager.py`,
  `generate_daily_report.py`). Flagging per AGENTS.md ("discover but don't
  fix in the same change") — do not fix inside this quick task if/when it's
  implemented; file separately.
- `mts_spread_events.jsonl` (`paper/logs/`) has the RELEASE decision records
  (trade_id, release_leg, decision ts, entries, sides) needed to seed each
  replay.

---

## 4. Required Engine (scope of future implementation)

1. **Input:** one historical RELEASE event (trade_id, release ts, near/far
   entry prices, sides, release_leg).
2. **Replay loop:** load near+far tick series from the release ts forward
   (same session, or until session boundary / next trading session per
   actual MTS session-continuation rules — SPREAD phase is allowed to carry
   overnight per ADR-006, so this must NOT artificially cut at session
   close unless that's also what the real strategy does).
3. **State machine:** reimplement (or directly call, via dependency
   injection / monkeypatch of market inputs) the exact Policy J
   activation → peak-tracking → giveback → confirm-debounce logic from
   `tmf_spread.py` — ideally by driving the real `_update_policy_j_peak()`
   / adjacent methods with replayed ticks rather than re-deriving the
   formula independently, to avoid a second implementation that silently
   drifts from production behavior.
4. **Terminal condition:** first tick where the replayed state machine
   would emit `COMBINED_EXIT`; record replay PnL at that point (net, with
   fee+tax applied the same way `COMBINED_EXIT_SETTLED.combined_realized_pnl_net`
   is computed in production — see this session's confirmed real-settlement
   field, not `EXIT_LOG.pnl`, which is a decision-time estimate per the
   confirmed lesson from 2026-11-19).
5. **Output:** per-trade `(actual_net, counterfactual_net, delta)` — same
   shape as this session's retracted analysis, but with a real "hold and
   wait" counterfactual instead of an "instant exit" one.

---

## 5. Validation Gate (must pass BEFORE trusting any replay output)

The replay engine must first prove it reproduces REALITY on cases where NO
release happened and Policy J fired normally: pick N≥10 historical
`COMBINED_EXIT` trades (no RELEASE in their lifecycle at all), feed the
engine their near+far tick series from entry, and confirm it reproduces the
real `COMBINED_EXIT` trigger timestamp and PnL within a tight tolerance
(exact trigger tick, or documented reason for any drift). **Do not run the
engine on release-suppression counterfactuals until this gate passes.** This
mirrors R-002's reproducibility-first discipline and directly guards against
repeating this session's two retracted mistakes.

---

## 6. Sample Plan (deferred until §5 passes)

- Minimum N for a direction-level read: ~30-50, matching this project's
  established VCP-era sample threshold (see memory: VCP conclusion required
  comparable N before any "not tradable" verdict).
- Report per-trade deltas AND the aggregate TWD delta — this session's
  mistake-within-a-mistake was almost drawing a conclusion from a count-based
  split (26 vs 13) that reversed under TWD-weighted aggregation. Always lead
  with the TWD aggregate, count split is secondary color.

---

## 7. Explicit non-goals for this spec

- Does NOT re-open ADR-027 (SINGLE_LEG post-release time-stop) — that
  question is CLOSED/H0 and out of scope here.
- Does NOT implement or flip `release_enabled` — that remains a separate,
  explicit, evidence-gated decision after this research produces a result.
- Does NOT fix the cwd-relative `raw_ticks`/`raw_kbars` path issue noted in
  §3 — recorded as a known, separate defect only.

---

## 8. Status

SPEC ONLY. No code written. Next step (when picked up): implement §5's
validation harness first; do not proceed to §4/§6 until §5 passes.
