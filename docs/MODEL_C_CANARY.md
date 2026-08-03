# Model C Canary — Synchronized BBO Executable Marking

**Status**: ACTIVE (2026-08-03, pending deploy approval)
**Type**: Observational telemetry (shadow only)
**Related**: ADR-025 (Policy J combined exit), `core/model_c_collector.py`,
`core/spread_shadow_collector.py` (P2), docs/LIVE_TRANSITION_SOP.md

## 1. Purpose

Rebuild trigger-time executable gross PnL from synchronized NEAR/FAR bid/ask,
so Policy J trigger marks can be audited against actually-executable prices.

```
execution_influence = false
order_influence = false
policy_threshold_influence = false
shadow_only = true
```

## 2. Price semantics (locked)

| position | close side | executable exit price |
|----------|-----------|------------------------|
| LONG     | SELL      | bid                    |
| SHORT    | BUY       | ask                    |

```
LONG:  (exit_bid - entry_avg) × qty × point_value
SHORT: (entry_avg - exit_ask) × qty × point_value
combined = near + far
```

Forbidden as executable: last trade, mid, bar close, signal price,
mixed last-trade/BBO.

## 3. Data & pairing

Quote snapshot per leg: bid/ask/sizes, exchange_ts, receive_ts, seq,
source, subscription_id. exchange_ts null -> timestamp_quality=RECEIVE_ONLY
(never fake exchange ts).

Pairing = quote-state snapshot (latest_near_bbo / latest_far_bbo), gated:

```
near_quote_age_ms <= 2000
far_quote_age_ms  <= 2000
pair_skew_ms      <= 500
bid > 0, ask > 0, ask >= bid
```

receive skew and exchange skew saved separately.

## 4. Episode inflation guard

133,859 rejections collapsed to ~12 stale episodes (P2 canary). Model C
episode key = reason + stale_leg + stale quote receive_ts. Same stale quote +
same reason -> attempt++, episode unchanged. New episode on: stale quote
update, reason change, recovery (accepted pair).

## 5. Telemetry events

- `BBO_UPDATE` (raw, per-leg)
- `MODEL_C_PAIR_ACCEPTED` (full pair + position context + executable PnL +
  model_b_mark_pnl for contrast; model_version=MODEL_C_V1, shadow_only=true)
- `MODEL_C_PAIR_REJECTED` (reason taxonomy: NEAR/FAR_BBO_MISSING,
  NEAR/FAR_STALE, BOTH_STALE, PAIR_SKEW_EXCEEDED, INVALID_NEAR/FAR_BOOK,
  TIMESTAMP_MISSING, POSITION_STATE_INCOMPLETE — never UNKNOWN)

## 6. Thresholds (observational, not gates)

Initial: max_quote_age_ms=2000, max_pair_skew_ms=500. Offline sweep:
age 100/250/500/1000/2000/5000ms, skew 50/100/250/500/1000/2000ms. Goal:
coverage vs executable accuracy vs event-to-fill predictiveness tradeoff —
not max acceptance. Never loosen to re-accept stale far quotes.

## 7. Acceptance gates (canary close)

- Collection threshold (either completes the accumulation phase):
  - >= 3 full trading days, OR
  - >= 100 POST_GUARD matched COMBINED_EXIT
  (2026-08-03 user decision: OR — do not wait for both)
- cross-contract contamination = 0; duplicate accepted = 0; UNKNOWN reason = 0
- accounting invariant failures = 0; execution influence = 0
- Model C reconstructable trigger ratio >= 90%
- Model C-to-realized median abs gap < current event-to-realized median abs gap
  (check p90/p95 tails, not just mean)

## 7b. Target Cases (validation checkpoints, 2026-08-03)

### TC-1: Opening-gap stale marking (recorded 2026-08-03)

**Trade**: `mts-auto-134026-806` — entry 13:40:26 (NEAR SHORT 43260 /
FAR LONG 43405), carried across day close, exited 15:00:00.077 (night open,
first tick) via POLICY_J_RELEASE_INTERCEPT -> COMBINED_EXIT (ORD-635/636).

**Observed three-layer mark discrepancy at the opening gap**:

| layer | value | source |
|-------|-------|--------|
| strategy ctx UPL mark | +348 TWD | mts_lifecycle_adapter ctx (stale spread mark) |
| trigger event (COMBINED_EXIT_SUBMITTED) | 44.0 pts = 440 TWD | mts_spread_events.jsonl |
| actual realized (fills) | +4,950 TWD (NEAR +2,600 / FAR -2,350) | mts_trade_fills.jsonl |

Mark vs actual ≈ 14x understatement at the opening tick. Policy J used the
pre-open/stale spread mark for the trigger evaluation.

**What this is / is not**:
- NOT a wrong-exit bug: exit used market orders -> realized +4,950 locked.
  Trigger condition (>= activation 200) held under both stale and accurate
  marks in this case -> same timing.
- IS an accuracy concern: stale mark can mis-time triggers in other cases
  (missed profit when understated; premature exit when overstated).
- Low frequency: 1/40 trades today.

**Decision (2026-08-03)**: do NOT change Policy J marking before Model C
validation (promotion gate: DO_NOT_REPLACE_MARKING_IN_EXECUTION_PATH).
Record as Model C acceptance target case instead.

**Model C validation check for TC-1**:
- Can Model C rebuild the opening-instant (15:00:00.000) executable mark
  (near/far bid-ask at first tick)?
- Does Model C executable mark track realized (+4,950) far better than the
  stale ctx mark (+348)?
- If yes -> evidence that executable BBO marking fixes opening-gap
  mis-marking; proceed to shadow decision comparison.

### TC-2: Far-leg release-stop intercept at open (same trade)
Far leg hit release stop while combined net >= activation -> COMBINED_EXIT
intercept. Verify Model C far-leg executable pnl (SHORT->ask) at the same
instant matches the realized far pnl (-2,350) within tolerance.

## 8. Verdicts (fixed until canary completes)

```
MODEL_A_REJECTED (LAST_TRADE_PAIRING_INPUT_INSUFFICIENT — 0.07/min far last trade)
MODEL_C_CANDIDATE (primary — Policy J executable semantics)
DO_NOT_PROMOTE_TO_REAL / DO_NOT_CHANGE_POLICY_J_THRESHOLDS /
DO_NOT_REPLACE_MARKING_IN_EXECUTION_PATH
```

## 9. File map

- `core/model_c_collector.py` — collector (pairing, episodes, telemetry,
  Model B contrast)
- `tests/core/test_model_c_collector.py` — 16 tests
- `data/telemetry/model_c/` — telemetry JSONL (per trading day)
- `data/model_c_canary.flag` — enable marker (dynamic check, no restart)
