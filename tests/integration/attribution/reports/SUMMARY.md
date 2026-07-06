# Attribution Analysis Summary

Generated: 2026-04-22 22:23:01

## Router Exposure Summary

| Strategy | Candidate Count | Evaluated | Winner | Shadowed | Starvation Index |
|----------|----------------|-----------|--------|----------|------------------|
| counter_vwap | 2,803 | 1,401 | 0 | 0 | 🟡 0.50 |
| kbar_feature | 2,802 | 1,401 | 0 | 0 | 🟡 0.50 |
| spring_upthrust | 2,802 | 1,401 | 0 | 0 | 🟡 0.50 |
| router | 1,401 | 0 | 0 | 0 | 🔴 1.00 |

## Trade Performance Summary

No trade data available.

## Starvation Analysis

Strategies with high starvation index may need priority adjustment:

| Strategy | Starvation Index | Level | Shadowed Count | Evaluation Count |
|----------|------------------|-------|----------------|------------------|
| router | 1.000 | 🔴 severe | 0 | 0 |
| counter_vwap | 0.500 | 🟡 moderate | 0 | 1,401 |
| kbar_feature | 0.500 | 🟡 moderate | 0 | 1,401 |
| spring_upthrust | 0.500 | 🟡 moderate | 0 | 1,401 |

## Recommendations

1. **Review high-starvation strategies**: Consider adjusting priority order
2. **Analyze shadowed strategies**: Check if they would have been profitable
3. **Monitor regime distribution**: Ensure strategies are evaluated in appropriate regimes
4. **Validate trade performance**: Compare router exposure with actual PnL
