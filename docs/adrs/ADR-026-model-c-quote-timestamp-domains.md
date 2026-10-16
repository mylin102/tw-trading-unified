# ADR-026: Model C quote timestamp domains and source validation status

Date: 2026-08-05
Status: ACCEPTED (source validation) — decision layer NOT IMPLEMENTED
Related: ADR-021 (execution and fill model), 2026-08-04 Model C review items

## Context

Model C was originally designed assuming the futures tick callback carries
bid/ask. Runtime capture proved this wrong: TMFH6/MXFH6 tick streams carry
only last/close (2,356 samples, bid=None/ask=None). The f3743daa fallback
(buy_price/sell_price) changed nothing because there was no data to fall
back to. Futures BidAsk (BidAskFOPv1) subscription was probe-verified
(sandbox: 735+ records/25s) and adopted.

Separately, a clock-domain hazard was discovered: BidAskFOPv1.datetime is a
naive TAIFEX local wall-clock. On Mini (TZ CST+0800) its .timestamp() shares
the UTC epoch contract with time.time(), but the TAIFEX server clock leads
Mini by ~85ms (91.5% of observed pairs had negative exchange age). Mixing
exchange and receive clocks for freshness gating is therefore unsafe.

## Decision

1. Futures near/far subscribe BidAskFOPv1; only BBO_VALID quotes feed
   Model C; tick/last NEVER becomes an executable quote fallback.
2. Canonical near/far contract identity (TMFH6/MXFH6 and TMFI6/MXFI6 are
   the same contracts; CONTRACT_MISMATCH blocks otherwise).
3. Clock domains are SEPARATED:
   - exchange_ts (BidAskFOPv1.datetime): naive TAIFEX local wall-clock, ms
     precision. Used ONLY for quote ordering, exchange pair skew, and
     observation. NEVER subtracted from Mini receive clock for freshness.
   - receive_ts: local receive time. Freshness gating uses receive-age only.
   - ANY negative exchange_quote_age_ms -> timestamp_quality =
     EXCHANGE_TS_CLOCK_DOMAIN_UNKNOWN and the age field is null. Negative
     ages are NEVER clamped to 0 (that would disguise an unknown clock
     offset as a fresh quote).
4. Model C currently produces executable-PnL telemetry only. There is NO
   decision consumer and NO execution path (execution_influence=false,
   order_influence=false). The canary flag controls observation only.
5. Status vocabulary:
   - BBO SOURCE: VERIFIED (probe + 75min observation window)
   - PAIRING: VERIFIED
   - EXECUTABLE PNL TELEMETRY: VERIFIED
   - EXCHANGE-TIME VALIDATION: PENDING (clock-domain classification active)
   - DECISION LAYER: NOT IMPLEMENTED
   - EXECUTION PROMOTION: NOT APPLICABLE

## Evidence (post-fix window 2026-08-04 16:38–17:53, 75 min)

- accepted pairs: 54,311 (96.2% of 56,459 pairing attempts)
- BidAsk source: 56,452 / 56,461 records (99.98%); 9 shioaji_tick records
  are pre-fix legacy and EXCLUDED from purity math
- reject reasons: PAIR_SKEW_EXCEEDED 2,117; FAR_STALE 22; NEAR_BBO_MISSING 1;
  FAR_BBO_MISSING 8
- quote age (receive-domain): p50=1.4ms, p95=23.9ms, max=522ms
- receive pair skew: p50=100.7ms, p95=371ms, max=500ms (cap)
- exchange pair skew: measured separately after exchange_ts propagation
- execution influence: false; order influence: false

## Consequences

- The canary flag (data/model_c_canary.flag) gates telemetry observation
  only. There is no "lift the canary to enable trading" action — a decision
  consumer does not exist.
- Any future decision layer MUST be a new ADR/gate; the source-validation
  status here does NOT auto-promote Model C to a trading input.
- Freshness gates use receive-age; exchange-age unknown degrades observation
  confidence only, never proves quote freshness.
- Telemetry volume: ~54k accepted pairs/75min, ~23MB bbo_raw + ~65MB
  model_c per session — bounded observation recommended (sampling/aggregates)
  before long-term retention.
