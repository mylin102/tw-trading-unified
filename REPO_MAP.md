<!-- generated-by: gsd-doc-writer -->
# Repository Map (REPO_MAP.md)

This document provides a logical map of the Taiwan Trading Unified repository to help developers and traders navigate the 100+ files.

---

## 🏗️ Core Infrastructure (`core/`)
The foundational layer for data management, session handling, and execution logic.

- `backtest_engine.py`: Unified event-driven simulator for high-fidelity backtesting.
- `shioaji_session.py`: Handles authentication and connection to the Shioaji (SinoPac) API.
- `data_manager.py`: Core interface for loading, cleaning, and storing market data.
- `strategy_base.py`: The abstract base class that all trading strategies must inherit from.
- `strategy_registry.py`: Dynamic registry for discovering and loading strategy plugins.
- `circuit_breaker.py`: Safety mechanism to stop trading during extreme market conditions.
- `signal.py` & `signal_processing.py`: Definitions and helpers for trade signals and indicators.
- `data_sentinel.py`: Background monitoring for data gaps and integrity.
- `market_regime.py`: Classification of market conditions (Trend, Range, Volatile).

---

## 📈 Assets & Execution (`strategies/`)
Asset-specific trading logic and execution monitors.

### Futures (`strategies/futures/`)
- `monitor.py`: Real-time monitoring and execution engine for futures.
- `elite_strategies.py`: Refined, production-ready futures strategies.
- `squeeze_futures/`: Specialized sub-module for Squeeze-based breakout strategies.

### Options (`strategies/options/`)
- `live_options_squeeze_monitor.py`: The main execution script for options trading.
- `theta_gang.py`: Strategy focused on time-decay (Theta) based selling.
- `options_engine/`: Internal engine for Greeks calculation and options-specific backtesting.

### Stocks (`strategies/stocks/`)
- `scanner.py`: Scans the market for stocks meeting specific technical criteria (e.g., CANSLIM).
- `multi_timeframe.py`: Signal generation using combined analysis of multiple timeframes.
- `pattern_engine.py`: Identification of classic chart patterns (VCP, Cup & Handle).

---

## 🧩 Strategy Plugins (`strategies/plugins/`)
Pluggable strategy modules that can be hot-swapped or optimized individually.

- `futures/`: Implementation of specific models like `orb_ml.py`, `vol_squeeze.py`, and `kalman_momentum.py`.
- `options/`: Option-specific plugins like `v2_squeeze.py`.

---

## 🧪 AI & Research Tools (`backtest/`, `scripts/optimization/`)
Advanced tools for strategy validation, parameter tuning, and ML training.

- `backtest/unified_runner.py`: The primary CLI tool for running cross-strategy comparisons.
- `backtest/monte_carlo.py`: Robustness testing using randomized walk-forward simulations.
- `scripts/optimization/annual_sweep.py`: Large-scale parameter optimization over historical years.
- `scripts/optimization/train_rf.py`: Training script for Random Forest models used in ORB-ML strategies.
- `scripts/backtest/`: Specialized scripts for testing specific hypotheses or new indicators.

---

## 🖥️ UI & Dashboards (`ui/`)
Streamlit-based interfaces for human-in-the-loop monitoring and research.

- `dashboard.py`: The main live trading cockpit (Positions, PnL, Real-time Charts).
- `backtest_dashboard.py`: The comprehensive research UI for analyzing backtest results.
- `backtest_pages/`: Modular tabs for the backtest UI (Sweep analysis, Single test, Optimization).

---

## 📚 Documentation & Reports (`docs/`, Root)
Project knowledge base and operational records.

- `docs/TECHNICAL_ARCHITECTURE.md`: Deep dive into the system design and data flow.
- `docs/LIVE_TRADING_GUIDE.md`: Essential checklist and manual for going live.
- `docs/SDD.md`: System Design Documents for major modules (Stocks, Pluggable Strategies).
- `AGENTS.md`: Roles and responsibilities for AI collaborators (Gemini, Claude, Qwen).
- `RULES.md`: Mandatory operational rules to prevent financial loss.

---

## 🛠️ Root Utilities
- `main.py`: The entry point for starting the unified trading system.
- `quick_start.sh`: Shell script to set up the environment and launch the dashboards.
- `requirements.txt`: Python dependency list.
- `autostart.sh` & `check_system_status.sh`: Maintenance and operational monitoring scripts.
