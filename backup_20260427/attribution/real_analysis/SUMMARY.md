# Attribution Analysis Summary

Generated: 2026-04-23 05:13:58

## Router Exposure Summary

| Strategy | Candidate Count | Evaluated | Winner | Shadowed | Starvation Index |
|----------|----------------|-----------|--------|----------|------------------|
| counter_vwap | 100 | 50 | 27 | 0 | 🟡 0.50 |
| spring_upthrust | 73 | 23 | 6 | 27 | 🟡 0.68 |
| kbar_feature | 67 | 17 | 1 | 33 | 🔴 0.75 |
| router | 16 | 0 | 0 | 0 | 🔴 1.00 |

## Trade Performance Summary

No trade data available.

## Starvation Analysis

Strategies with high starvation index may need priority adjustment:

| Strategy | Starvation Index | Level | Shadowed Count | Evaluation Count |
|----------|------------------|-------|----------------|------------------|
| router | 1.000 | 🔴 severe | 0 | 0 |
| kbar_feature | 0.746 | 🔴 severe | 33 | 17 |
| spring_upthrust | 0.685 | 🟡 moderate | 27 | 23 |
| counter_vwap | 0.500 | 🟡 moderate | 0 | 50 |

## Recommendations

1. **Review high-starvation strategies**: Consider adjusting priority order
2. **Analyze shadowed strategies**: Check if they would have been profitable
3. **Monitor regime distribution**: Ensure strategies are evaluated in appropriate regimes
4. **Validate trade performance**: Compare router exposure with actual PnL
