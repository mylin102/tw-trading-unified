# Attribution Data Collection Summary

**Collection Period**: 2026-04-23 00:03:48 to 2026-04-23 00:03:48

**Total Bars Processed**: 300

## Strategy Evaluations

| Strategy | Evaluations |
|----------|-------------|
| counter_vwap | 300 |
| spring_upthrust | 300 |
| kbar_feature | 300 |

## Generated Files

- **strategy_signal_log.csv**: 0 rows, 0.1 KB
  - Columns: timestamp, symbol, regime, strategy_name, candidate_order...
- **router_evaluation_log.csv**: 900 rows, 88.3 KB
  - Columns: timestamp, symbol, regime, strategy_name, candidate_order...
- **trade_attribution_log.csv**: 0 rows, 0.1 KB
  - Columns: trade_id, symbol, strategy_name, regime_at_entry, side...

## Next Steps

1. Run attribution report:
   ```bash
   python scripts/attribution_report.py --input-dir /Users/mylin/Documents/mylin102/tw-trading-unified/data/attribution/collection --output-dir /Users/mylin/Documents/mylin102/tw-trading-unified/data/attribution/collection/reports
   ```

2. Check for starvation:
   ```bash
   python scripts/starvation_alerts.py --input-dir /Users/mylin/Documents/mylin102/tw-trading-unified/data/attribution/collection --threshold 0.7
   ```

3. Run reorder simulation:
   ```bash
   python docs/strategy_reorder_simulator.py --input-dir /Users/mylin/Documents/mylin102/tw-trading-unified/data/attribution/collection
   ```
