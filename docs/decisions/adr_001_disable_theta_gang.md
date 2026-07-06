# ADR-001: Disable ThetaGang Strategy

**Status:** Accepted (2026-04-24)  
**Scope:** OptionsMonitor, ThetaGang execution path

## Context

ThetaGang sells iron condors and credit spreads in ranging markets. It was designed as a premium-collection strategy for low-volatility environments.

## Decision

ThetaGang is disabled. The `use_theta` flag is hardcoded to `False` in `live_options_squeeze_monitor.py`:

```python
use_theta = False  # DISABLED: theta gang has no edge vs friction (68pts)
```

## Rationale

- Friction cost for a round-trip options combo is ~68 points (4 legs × ~17 pts each)
- Average credit collected was ~50-80 points per trade
- Net edge after friction: near zero or negative
- ThetaGang was competing with directional signals for the same capital

## Consequences

- Directional signals (CALL/PUT) have full priority
- No premium-selling strategies active
- ThetaGang code remains in the codebase but is dead code

## Related

- `docs/architecture/system_overview.md`
- `strategies/options/live_options_squeeze_monitor.py` line ~4198
