# ADR-014: Stock CANSLIM & RS Rating Integration Status & Roadmap

- **Date**: 2026-07-13
- **Author**: Gemini CLI
- **Status**: Proposed / Pending Implementation

## Context

During a system audit on 2026-07-13, we analyzed the integration status of the Relative Strength (RS) rating and CANSLIM alpha signals from `tw-canslim-web` into the `tw-trading-unified` stock trading system. 

We found that while the data ingestion and filtering infrastructure are fully implemented and verified via unit tests, the active trading monitor and strategies are currently bypassed and rely on a static watchlist and pure technical indicators.

This document records the exact state of this integration and outlines the necessary steps to complete it.

## Current Integration State

### What is Done (Verified Infrastructure)
1. **External Feature Ingestion**: [external_feature_provider.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/core/external_feature_provider.py) is implemented to fetch `leaders.json` from the remote repository.
2. **Quality & Defense Filters**: Filter rules in `_is_valid_leader()` successfully drop low-quality codes (ETFs, `rs_rating <= 0`, `industry_rank >= 999`, and `min_atr_pct`).
3. **Sorting & Selection**: Watchlist symbol sorting is configured by `industry_rank`, then `-rs_rating`, then `-composite_score`.
4. **Unit Test Coverage**: [test_external_feature_provider.py](file:///Users/mylin/Documents/mylin102/tw-trading-unified/tests/test_external_feature_provider.py) has 100% pass rate.

### What is Pending (Bypassed Components)
1. **Dynamic Watchlist Sync in Monitor**: The stock trading monitor [monitor.py (stocks)](file:///Users/mylin/Documents/mylin102/tw-trading-unified/strategies/stocks/monitor.py) still loads a static `watchlist` from [stocks.yaml](file:///Users/mylin/Documents/mylin102/tw-trading-unified/config/stocks.yaml) and does not call `ExternalFeatureProvider.get_snapshot()` to dynamically update tickers.
2. **Configuration Settings**: [stocks.yaml](file:///Users/mylin/Documents/mylin102/tw-trading-unified/config/stocks.yaml) is missing the `external_features` configuration block (defaulting `enabled` to `false`).
3. **Position Sizing Strategy integration**: The dynamic position sizing and scaling rules based on the RS/CANSLIM score (as defined in [EXTERNAL_STOCK_ALPHA.md](file:///Users/mylin/Documents/mylin102/tw-trading-unified/docs/EXTERNAL_STOCK_ALPHA.md)) are not implemented in the entry strategies.
4. **Analysis Modules**: The `IndustryMomentumAnalyzer` and `CompositeRankingEngine` referenced in the roadmap ([strategies.md](file:///Users/mylin/Documents/mylin102/tw-trading-unified/docs/strategies.md)) only exist as pseudocode.

## Next Steps Roadmap

1. **Phase 1: Config & Monitor Wiring**
   - Add `external_features` block to [stocks.yaml](file:///Users/mylin/Documents/mylin102/tw-trading-unified/config/stocks.yaml).
   - Update `self.watchlist` initialization in [monitor.py (stocks)](file:///Users/mylin/Documents/mylin102/tw-trading-unified/strategies/stocks/monitor.py) to dynamically fetch and refresh watchlist symbols when enabled.
2. **Phase 2: Strategy Integration**
   - Implement `evaluate_alpha_multiplier` in `strategies/stocks/entry_strategies.py` to scale positions dynamically based on composite scores.
   - Wire membership stop exits if a stock falls off the dynamic watchlist.
3. **Phase 3: Backtest Validation**
   - Run backtests comparing technical-only strategy against CANSLIM-filtered strategy to verify performance impact.
