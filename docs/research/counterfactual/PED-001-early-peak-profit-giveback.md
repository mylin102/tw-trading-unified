# Counterfactual Experiment PED-001

## Status

WAITING_FOR_MODEL
```
Target trades:      1 (38-677)
Reviewed trades:    18
Target for analysis: 100
```

## Summary

This experiment evaluates whether alternative exit models could
preserve more of the early favorable excursion than the baseline
``ATR_DYNAMIC`` engine.

The anchor trade (38-677) reached +692 within 7 seconds of entry
but exited at -1848 forty seconds later, yielding a Peak-to-Exit
Drawdown (PED) of 2540 TWD.

## Experiment Design

### Baseline

```
exit_engine: ATR_DYNAMIC
```

### Candidate Exit Models

| Model | Logic |
|-------|-------|
| BREAK_EVEN | After MFE >= threshold, move stop to entry |
| MFE_DECAY | Exit when PED >= N% of MFE |
| PROFIT_LOCK | Lock profit at tiered thresholds |
| TIME_CEILING | Exit if no new MFE high within N minutes |

### Metrics

- ``final_pnl``
- ``PED`` (MFE - FinalPnL)
- ``MFE`` / ``MAE``
- ``holding_time``
- ``exit_type``

## Target Trades

- [38-677](../trade-reviews/trade-38-677.atr_dynamic_ped.md)

## Hypothesis

> H_PED_001: The ATR_DYNAMIC exit engine permits excessive
> profit give-back on trades that peak early, because the
> trailing stop is calibrated to volatility rather than to
> the actual favorable excursion on the open position.

## Evidence Queue Threshold

This experiment will be re-evaluated when the trade-review
dataset reaches **100 reviewed trades** with clean PED data.

## Related Research

- PED metric definition and rationale (this doc)
- [Trade 38-677 review](../trade-reviews/trade-38-677.atr_dynamic_ped.md)
