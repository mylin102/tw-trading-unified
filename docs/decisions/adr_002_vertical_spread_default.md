# ADR-002: Vertical Spread as Default Options Execution

**Status:** Accepted (2026-04-28)  
**Scope:** OptionsMonitor, entry execution path  

## Context

Options directional signals (CALL/PUT) were originally executed as single-leg option purchases. A single-leg option has unlimited risk on the short side and high capital cost on the long side. Vertical spreads cap risk and reduce margin requirement.

## Decision

All directional CALL/PUT signals are converted to debit vertical spreads by default:

```
CALL signal → Bull Call Spread: Buy ATM Call, sell OTM Call (strike + width)
PUT signal  → Bear Put Spread: Buy ATM Put, sell OTM Put (strike - width)
```

Controlled by config flag `vertical_spread.enabled` in `options_strategy.yaml`:

```yaml
vertical_spread:
  enabled: true
  width: 100  # strike width in points
```

## Rationale

- Capped risk: max loss = net debit paid
- Lower margin: defined-risk spreads require less collateral
- Reward/risk gate: `select_vertical_spread()` rejects spreads with R/R < 1.5
- Bid/ask quality gate: rejects spreads with spread/mid > 0.30
- Friction gate: edge must exceed estimated friction cost

The selector is in `strategies/options/spread_selector.py`; tested via `tests/test_spread_selector.py` (20 tests).

## Fallback

If the spread selector rejects a signal (e.g., reward/risk too low, bid/ask too wide), execution falls back to single-leg entry with a logged skip reason. This prevents the system from being stuck when markets are illiquid.

## Consequences

- All paper and live entries go through the spread selector
- Single-leg entry is only used when spread rejection is logged and visible
- Tests verify all edge gates: 20 tests, all passing

## Related

- `docs/architecture/system_overview.md` — Options Execution Flow
- `strategies/options/spread_selector.py` — selector implementation
- `strategies/options/live_options_squeeze_monitor.py` — `enter_spread_paper_position()` and `enter_spread_live_position()`
- `config/options_strategy.yaml` — config flag
