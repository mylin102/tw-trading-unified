# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.21.0.0] - 2026-05-02

### Added
- **Breakout Engine V2 (v1.5)**: Implemented ATR-normalized breakout strength logic `(Close - High20.shift(1)) / ATR`. Replaced static percentage-based breakout thresholds.
- **Fail-Safe Entry Mechanism**: Integrated emergency entry logic in `OptionsMonitor` to force trades during primary engine crashes if high-confidence signals are detected.
- **Automated Audit Tool**: New `scripts/v15_daily_audit.py` for daily performance summarization (ATR gate status, session buffers, PnL).
- **Market Data Normalizer**: Unified entry point for all API data in options monitor to strictly enforce float casting and detect `Decimal` pollution.

### Changed
- **Three-Stage Breakout Logic**: Upgraded classification to a multi-axis gate: Structure (Price High) -> Strength (ATR > 0.25) -> Confirmation (Volume Spike >= 1.5 + VWAP Alignment).
- **Regime-Aware Thresholds**: Dynamic scaling of breakout sensitivity (0.15 ATR in `TRENDING`, 0.25 ATR in `SQUEEZE`).
- **Dashboard V2**: Expanded futures metrics to 6 columns, adding real-time "Breakout Strength" with directional emojis (🚀/💀).

### Fixed
- **Critical Type Crash**: Fixed `unsupported operand type` error caused by `decimal.Decimal` objects from Shioaji `on_bidask` callback.
- **Holiday Detection**: Updated `core/date_utils.py` to correctly identify TAIFEX holidays (e.g., Labor Day) and weekend closures to prevent unnecessary reboots.
- **Sign Inversion**: Corrected `DirectionLock` logic where positive scores were incorrectly interpreted as bearish.
- **Session Reset**: Ensured `bars_since_open` counter resets precisely at 15:00 for the night session to prevent opening-bar volume miscalculation.

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
