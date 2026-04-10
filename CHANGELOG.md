# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.20.0.0] - 2026-04-10

### Added
- **CANSLIM Pattern Engine**: New module `pattern_engine.py` using `argrelextrema` and smoothed price analysis to detect "Cup with Handle" and "Double Bottom" bases.
- **Geometric Test Suite**: Integrated `test_pattern_engine.py` with a synthetic pattern generator for high-confidence geometric validation.
- **Daily Data Pipeline**: Updated `downloader.py` and `scanner.py` to support 1-year historical daily data required for CANSLIM analysis.
- **Breakout Strategy**: `strategy_stock_canslim_breakout` in `entry_strategies.py` with volume-surge validation (1.4x multiplier) and pivot-point entry logic.

### Changed
- **Multi-Timeframe Scanner**: `StockScanner` now performs dual-track analysis: Daily for base building and 5m for intraday execution.
- **Stock Monitor Integration**: `StockMonitor` now caches daily pattern results once per day, keeping the execution loop lean and high-performance.
- **Config Driven Patterns**: CANSLIM parameters (cup depth, handle length, etc.) are now fully configurable via `config/stocks.yaml`.

### Fixed
- **Manual Resampling**: Resolved `api.kbars` interval incompatibility by implementing robust Pandas resampling from 1m to 5m/1d.
- **Data Continuity**: Fixed zero-volume stagnation in TMF night sessions using Virtual Ticks from MTX.
- **Boolean NAN Bug**: Corrected indicator logic where `np.nan` was being treated as `True` in signals.
- **Risk Management**: Implemented `opening_grace_mins` and `entry_premium_limit` to prevent losses during high-volatility opening spikes.
