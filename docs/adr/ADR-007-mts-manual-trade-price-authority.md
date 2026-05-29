# ADR-007: MTS Manual Trade Price Authority

**Date**: 2026-05-20
**Status**: Accepted
**Context**: Multiple production bugs caused MTS manual spread entry to use stale
hardcoded fallback prices (41800/41900) instead of live market tick data.

## Decision

### Price Authority Chain

```
Dashboard flag = intent only, NOT trusted price source
Monitor = execution authority, MUST revalidate with LIVE_TICK
```

### Rules

1. **Dashboard** writes flag with `trusted_price: false`.
   - Price source priority: MTS_STATE (live tick) → INDICATOR_CSV → reject
   - No hardcoded fallback prices allowed.

2. **Monitor** owns all execution prices.
   - Live mode (`live_trading=true`): MUST use `LIVE_TICK`, reject if unavailable.
   - Paper mode (`live_trading=false`): may fall back to `FLAG_FALLBACK` with
     explicit `price_source` tag.

3. **State Authority**:
   - Paper mode: `PaperTrader` is single source of truth for position state.
   - Live mode: Broker / `order_mgr` is single source of truth.
   - Paper mode MUST NOT read broker position on startup.

### Price Source Canonical Set

```
LIVE_TICK, PAPER_SIM, HISTORICAL_BAR, FLAG_FALLBACK,
SYNTHETIC_CONFIG, BACKFILL_BAR, INDICATOR_CSV,
MTS_STATE, MISSING, UNSET
```

### State Lifecycle (MTS Entry)

```
OPEN:
  _lifecycle = "OPEN"
  _released_leg = None    # Not an error — legal initial state
  _side = None            # Set on release, not at entry
  _peak/_nadir = entry price

After release:
  _released_leg = "near" | "far"
  _lifecycle = "TRAILING_LONG" | "TRAILING_SHORT"
  _side = remaining leg side
```

### Contract Tests

- `test_no_magic_price_fallback.py` — scans for forbidden magic numbers
- `test_no_get_numeric_fallback.py` — AST analysis of .get(key, numeric) in execution paths
- `test_price_provenance.py` — validates price_source against canonical set
- `test_mts_entry_state.py` — validates lifecycle state after sync_position()

## Consequences

- Dashboard cannot silently inject wrong prices into execution path.
- Monitor catches and rejects stale flag prices before order submission.
- Paper/Live mode boundary prevents broker state contamination.
- Contract tests prevent regression by agent or human.
- Existing `.get("near_entry", 0)` patterns in state restore code are known debt,
  tracked separately.
