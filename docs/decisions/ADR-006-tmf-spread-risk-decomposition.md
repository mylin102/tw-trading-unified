# ADR-006: TMF Spread Strategy Risk Decomposition

## Status
Accepted

## Date
2026-05-14

## Context

The TMF calendar spread strategy (`tmf_spread`) has been decomposed into a clear three-phase lifecycle:

1. **Entry (Phase 1)**: Near-far calendar spread entry (two legs, direction from spread_z sign)
2. **Release (Phase 2)**: Losing leg stopped out at `release_stop_points` (20pt default)
3. **Trail (Phase 3)**: Remaining leg exits via trailing stop (`trail_distance_points`, 30pt default)

A parameter sweep over today's spread CSV (2614 rows, 60 combos) showed:

- Release_stop=20 has the best risk containment (PF=1.17, final equity=88,850)
- Larger release_stop (60/80) worsens PF (1.08/1.05) and increases max DD (107%/116%)
- All parameter combinations lose money on today's data (final equity < 100,000)
- Max DD exceeds 100% for all combos — indicating position sizing or compounding issues

## Decision

### Parameter freeze for next observation period

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| release_stop_points | 20 | Fastest loss containment; larger stops let bad leg run |
| trail_distance_points | 30 | Reduces premature exit vs 20pt, no significant PF penalty |

### Known risk: Phase 3 converts to directional exposure

After release, the remaining leg becomes a naked directional futures position protected only by a trailing stop. This is the single largest risk in the strategy.

**Current protections:**
- Trailing stop (trail_distance_points)
- No re-entry mechanism (one-shot per entry)

**Missing protections (deferred):**
- Post-release max hold bars (time-stop)
- Partial hedge after release
- Delta exposure cap
- Volatility-scaled trail

## Alternatives Considered

### release_stop_points = 60
- PF dropped from 1.17 to 1.08
- Max DD increased from 105% to 107%
- Strategy edge insufficient to support larger stop
- Rejected

### trail_distance_points = 20
- Slightly higher PF (1.17 vs 1.15-1.08 for 30-50)
- But increases premature exit risk in extended runs
- Deferred — revisit if 30pt causes excessive retracement

## Consequences

- Release at 20pt will frequently trigger on noise (today's data showed 91 entries with 91 releases)
- Post-release directional exposure persists until trail exits
- All PnL metrics negative on today's data — strategy needs multi-day evaluation
- Backtest skeleton is ready for broader parameter sweeps across multiple days

## Future Research Topics (ordered by priority)

1. **Post-release max_hold_bars**: Time-stop on naked directional leg after release
2. **Partial hedge after release**: Reduce directional exposure
3. **Delta exposure cap**: Maximum naked directional risk
4. **Volatility-scaled trail**: Trail distance proportional to ATR
5. **Dynamic re-entry**: Mean-reversion re-entry after spread normalizes
6. **Asymmetric release thresholds**: Different stop for long vs short legs
