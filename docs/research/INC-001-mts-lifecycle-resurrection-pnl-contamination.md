# INC-001: MTS Lifecycle Resurrection and PnL Contamination Incident

**Status:** Root cause confirmed, P0 fixes in progress
**Date:** 2026-07-21 (occurred), 2026-07-23 (analysis completed)
**Evidence Level:** E3 (static code trace + runtime events + fill log)
**References:**
  - R-004 (MTS Entry Feature Dependency Audit)
  - ADR-009 (Position Lifecycle OCA)
  - ADR-010 (Broker-Level Release OCO)
  - `tmf_spread.py`, `core/spread_loader.py`, `strategies/futures/monitor.py`

---

## Incident Summary

MTS reported -448,696 total PnL after moving to Mini. Of this, -446,049 (99.6%) came
from a single trade (`mts-auto-115802-075`) due to PnL calculation contamination.

After excluding 5 trades with lifecycle anomalies (multi-release, multi-exit, PnL
overflow), the remaining 110 clean trades show +2,561 total PnL — the strategy was
marginally profitable overall.

However, the clean trades reveal a severe degradation pattern after the move to Mini:
62% win rate before → 15% win rate on Jul 22.

---

## Root Cause Classification

### Primary Root Cause

**Lifecycle terminal-state enforcement failure.** After a trade completed CLOSED
(entry → release → exit), the system allowed the same `trade_id`/lifecycle instance
to enter RELEASE and EXIT again. No irreversible terminal state, no duplicate
submission guard, and no quantity-based exposure invariant.

### Secondary Root Cause

**Accounting accepted invalid lifecycle events.** PnL calculation did not verify
execution uniqueness, remaining open quantity, or valid cost basis. Ghost fills
were booked into realized PnL.

### Contributing Factors

1. **Calendar spread pipeline stale** — Mini CSV stopped at Jul 20. All entries
   on Jul 22 used spread_z=3.0 (a fixed stale value), causing frequency explosion
   (34 trades/day, 15% win rate).

2. **Restart recovery downgrades UNKNOWN to FLAT** — `_restore_position_state()`
   failure sets `_has_position=False`, treating unprovable state as confirmed flat.
   Should be three-valued: FLAT / NON_FLAT / UNKNOWN.

3. **Single mutable snapshot for state** — `/tmp/mts_position_state.json` is
   vulnerable to reboot, crash, overwrite, and partial write.

4. **Order identity lacks durable idempotency** — No persistent idempotency key
   registry. `order_id` collisions possible after restart.

5. **Cross-ticker CSV discovery** — Dashboard glob selected MTX files instead
   of TMF. (Air4 only; Mini impact on strategy engine unconfirmed.)

### Non-Root Causes

- ATR, TRAIL, or entry threshold parameter values
- Actual market loss of 448k (the -446k is a calculation artifact)
- Dashboard display error alone (real PnL contamination existed in fills data)
- Shioaji timeout alone (contributing to CSV staleness but not the lifecycle bug)

---

## Event Chain

```
Layer 1: CSV stale on Mini
    fetch_calendar_spread_data.py cron → Shioaji TimeoutError → CSV stops at Jul 20
    (Confirmed: file listing, cron log)

Layer 2: Entry at fixed spread_z=3.0 (probable)
    All 115 entries show spread_z=3.0. Normal spread_z varies between -3.0 and +3.0.
    With stale CSV, forward-fill (_get_row_at searchsorted) returns last available value.
    If that value happens to be ~3.0, every on_bar() triggers entry.
    (Probable: pending file provenance evidence at entry time)

Layer 3: Normal first lifecycle (verified via events)
    ENTRY(11:58) → RELEASE(13:35, NEAR -1357.7) → EXIT(13:40, FAR +1112.2)
    Trade should be CLOSED at 13:40 with spread PnL -187.7

Layer 4: Lifecycle resurrection (core bug)
    13:44:54 — RELEASE fires again on same trade_id (NEAR -2257.7)
    13:44:54 — EXIT fires again on same trade_id (FAR +2052.2)
    15:00:48 — RELEASE fires again with reused order_id ORD-20260721-000011
    (Same order_id as the first release at 13:35!)
    Result: -446,048.9 realized PnL — a calculation artifact, not market loss.

Layer 5: State recovery fail-open
    _mts_position_state.json at /tmp/mts_position_state.json
    After restart, _restore_position_state() fails
    → _has_position = False (treats UNKNOWN as FLAT)
    → No reconciliation, no entry block
    (Contributing: exact cause of state loss unconfirmed — could be /tmp cleanup,
     partial write, or path mismatch)
```

---

## Evidence Verification Status

| Layer | Status | Evidence |
|---|---|---|
| CSV stale | CONFIRMED | File mtime, cron log with TimeoutError |
| Partial CSV consumption | PROBABLE | All entries at spread_z=3.0; needs file provenance at entry time |
| First lifecycle (normal) | CONFIRMED | Fill events show clean RELEASE → EXIT sequence |
| Lifecycle resurrection | CONFIRMED | Core evidence: same trade_id, multiple RELEASE/EXIT, reused order_id |
| /tmp state loss | UNCONFIRMED | Need STATE_RESTORE_ATTEMPT instrumentation |
| Cross-ticker selection | CONFIRMED (Air4) | Dashboard log shows mtx selection; Mini strategy engine unconfirmed |
| Dash- board stale data | CONFIRMED (Air4) | Log shows wrong file; Mini log not checked |

---

## Invariants Missing

The following invariants must be added to prevent recurrence:

### Invariant 1 — CLOSED terminal state is irreversible

Once `trade.lifecycle_status == CLOSED`, no ENTRY, RELEASE, EXIT, or order intent
may be committed for the same `lifecycle_epoch`.

### Invariant 2 — At most one committed RELEASE per lifecycle epoch

```python
assert count(committed RELEASE intents) <= 1
```

Count committed intents, not broker callbacks (which may be duplicated).

### Invariant 3 — Each leg can be closed at most once

```python
assert closed_qty <= opened_qty
```

### Invariant 4 — Accounting does not trust strategy events

PnL must verify:
- `trade_id`, `order_id`, `execution_id`, `leg`, `side`, `fill_qty`
- `previous_open_qty`, `remaining_qty`
- Fill uniqueness (duplicate execution_id → reject)

```python
realizable_qty = min(fill_qty, current_open_qty)
if realizable_qty <= 0:
    record_anomaly("ZERO_EXPOSURE_FILL")
    do_not_book_pnl()
```

### Invariant 5 — FLAT derived from position ledger

```python
has_position = position_ledger.net_open_quantity != 0
```

Eliminate `_has_position` as independent truth source.

---

## Accepted Corrections to Prior Analysis

| Claim | Previous version | Corrected version |
|---|---|---|
| PM2 restart clears /tmp | "/tmp cleared on restart" | Unconfirmed; state loss cause unknown |
| Partial CSV caused spread_z=3.0 | Asserted as fact | "Probable, pending input provenance" |
| Fix order_id with timestamp | "Add timestamp to order_id" | "Durable idempotency key registry needed" |
| Ghost PnL detection as P2 | Recommend as P2 | Must be P0 accounting invariant |

---

## P0 Fix Priority

1. **Isolate contaminated PnL** — Keep raw events; mark `mts-auto-115802-075`
   second/third sequences as `LIFECYCLE_ANOMALY`; exclude from stats.

2. **CLOSED terminal invariant** — Hard gate: CLOSED trade cannot receive
   new execution intents.

3. **Durable order idempotency** — `idempotency_key = trading_day + trade_id +
   lifecycle_epoch + action_type + leg + sequence`. Persist `COMMITTED` intents.

4. **Quantity-based position ledger** — Replace mutable `_has_position` with
   ledger-derived net open quantity.

5. **Accounting fail-closed** — Duplicate fills, zero-exposure fills, PnL
   exceeding economic bounds → quarantine, do not book.

6. **Restart reconciliation** — `UNKNOWN` state blocks entry until broker or
   durable paper ledger confirms flat. Three-valued: FLAT / NON_FLAT / UNKNOWN.

7. **Data freshness gate** — Entry requires:
   - `source_ticker == configured_ticker`
   - `data_age <= threshold`
   - `spread_z` changes between bars (edge-trigger, not level-trigger)

8. **Dashboard monitoring** — Stale data warning, cross-ticker detection,
   lifecycle anomaly dashboard panel.

---

## Acceptance Criteria

```
AC-01 CLOSED trade permanently rejects RELEASE/EXIT
AC-02 At most one committed RELEASE per lifecycle epoch
AC-03 Same idempotency key survives restart without re-submission
AC-04 Restore failure produces UNKNOWN state, blocks entry
AC-05 Local/broker or paper-ledger mismatch blocks entry
AC-06 closed_qty never exceeds opened_qty
AC-07 Duplicate fill does not double-book PnL
AC-08 Stale/partial/cross-ticker CSV does not produce entry signal
AC-09 Same signal generation does not produce repeated entries
AC-10 All PnL reconstructible from fills + quantity + multiplier + fees
AC-11 Raw contaminated events preserved; corrected data via correction layer
AC-12 Air4 and Mini output host/repo/commit/state/data provenance
```

---

## Corrected Dataset

```json
{
  "correction_type": "PNL_CONTAMINATION_EXCLUSION",
  "trade_id": "mts-auto-115802-075",
  "canonical_release_sequence": 1,
  "canonical_exit_sequence": 1,
  "excluded_event_sequences": [2, 3],
  "reason": "POST_CLOSE_LIFECYCLE_RESURRECTION",
  "original_reported_pnl": -446048.9,
  "corrected_trade_pnl": -187.7
}
```

Also exclude:
- `mts-auto-092556-978` (multi_release, -3124)
- `mts-auto-150532-155` (multi_release, -2324)
- `mts-auto-215210-900` (multi_release, +146)
- `mts-auto-101308-414` (odd_entries=0, +545)

---

## Files Modified During This Investigation

| File | Change | Status |
|---|---|---|
| `ui/dashboard.py` | Ticker-scoped spread discovery, cache invalidation | Air4 deployed, Mini deployed |
| `scripts/update_calendar_spread.py` | Added `--ticker` CLI override | Air4 committed, Mini patched |
| Crontab (both) | Switched to atomic writer | Air4 done, Mini done |

---

## Near-term Work Items

- [ ] INC-001 document (this file) accepted as baseline
- [ ] P0-A: Isolate contaminated PnL in downstream analytics
- [ ] P0-B: CLOSED terminal invariant guard
- [ ] P0-C: Durable idempotency key registry
- [ ] P0-D: Accounting fail-closed (qty bounds, PnL bounds)
- [ ] P1: State file path hardened (env-driven, not /tmp)
- [ ] P1: Restart reconciliation (UNKNOWN → ENTRY_BLOCKED)
- [ ] P1: Data freshness gate (edge-triggered entry, age threshold)
- [ ] P2: Cross-host provenance (`.deployment-target`, preflight)
- [ ] P2: Dashboard lifecycle anomaly panel
