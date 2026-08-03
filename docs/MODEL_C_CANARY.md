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

- >= 3 full trading days AND >= 100 POST_GUARD matched COMBINED_EXIT
- cross-contract contamination = 0; duplicate accepted = 0; UNKNOWN reason = 0
- accounting invariant failures = 0; execution influence = 0
- Model C reconstructable trigger ratio >= 90%
- Model C-to-realized median abs gap < current event-to-realized median abs gap
  (check p90/p95 tails, not just mean)

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
