<!-- generated-by: gsd-doc-writer -->
# 🇹🇼 Taiwan Trading Unified (V2 - AI Quant Lab)

[![Engineering Rigor](https://img.shields.io/badge/Engineering-V--Model-blue)](docs/V_MODEL_TEST_PLAN.md)
[![Data Engine](https://img.shields.io/badge/Data-Parquet--SSOT-green)](core/data_manager.py)
[![ML Pipeline](https://img.shields.io/badge/AI-Random--Forest-orange)](scripts/optimization/train_rf.py)
[![Architecture](https://img.shields.io/badge/Architecture-Modular-red)](docs/TECHNICAL_ARCHITECTURE.md)

A professional AI-driven quantitative trading and research platform for the Taiwan market (Futures, Options, and Stocks). This project has evolved through 17+ Waves of strategic refactoring into a high-performance system prioritizing capital protection and runtime determinism.

一個專為台灣市場（期貨、選擇權、股票）設計的專業 AI 驅動量化交易與研發平台。經過 17 個階段（Waves）的架構重構，本系統已演進為一個強調資金保護與執行確定性的高效能交易架構。

---

## 🌟 Core Features | 核心特點

### 1. AI Quant Lab (Wave 17+) | 量化實驗室
*   **Unified Runner**: A single entry point (`backtest/unified_runner.py`) for fair performance comparison across all asset classes (Futures, Options, Stocks).
*   **Parameter Optimization**: Multi-core grid search and Bayesian optimization for strategy tuning.
*   **Risk Analytics**: Integrated Monte Carlo stress testing and 95% VaR (Value at Risk) analysis.
*   **物理引擎**: Kalman Filter (降噪) 與 LRL Curvature (加速度) 向量化指標。

### 2. Squeeze Stock System | 強勢股擠壓策略
*   **Volatility Compression**: Real-time detection of TTM Squeeze and volatility expansion patterns optimized for Taiwan equities (`strategies/stocks/squeeze_patterns.py`).
*   **Multi-Timeframe Analysis**: Correlation checks between daily and 5-minute charts to confirm breakout strength.

### 3. CANSLIM Integration | CANSLIM 成長股引擎
*   **Technical Screening**: Automated detection of "Cup and Handle" patterns and volume-confirmed breakouts (`strategies/stocks/pattern_engine.py`).
*   **High-Growth Focus**: Logic-driven entry strategies based on CANSLIM growth principles tailored for the TWSE/TPEx markets.

### 4. Strategic Optimization Plan | 系統優化藍圖
*   **Capital Protection**: Rigorous multi-wave roadmap (Wave 0-4) focusing on runtime stability, asset parity, and regression prevention.
*   **Deterministic Execution**: Standardized session handling and date mapping to prevent session-date misalignment.
*   See the detailed roadmap in the project documentation for ongoing development phases.

---

## 🏗️ Architecture | 技術架構

The system follows a modular, pluggable "V-Model" architecture. For a deep dive into the components, data flow, and directory rationale, see:

**👉 [docs/TECHNICAL_ARCHITECTURE.md](docs/TECHNICAL_ARCHITECTURE.md)**

---

## 🚀 Quick Start | 快速開始

1.  **Environment Setup**:
    ```bash
    pip install -r requirements.txt
    # Create .env from .env.example with your Shioaji credentials
    ```
2.  **Launch Dashboard**: 
    ```bash
    bash autostart.sh  # Starts the Streamlit Dashboard at localhost:8501
    ```
3.  **Run Research**: 
    ```bash
    python3 backtest/unified_runner.py  # Compare all strategies across asset classes
    ```
4.  **Optimize Parameters**: Navigate to the "Stock Optimizer" page in the Dashboard.

---

## 🛠️ Engineering Heritage | 技術遺產 (Wave 1-17)
- **Unified Strategy Registry**: Automatically discovers and loads plugins from `strategies/plugins/`.
- **Data Engine**: High-performance 3-year (800k+ bars) 5-minute Parquet database with vectorized access.
- **Safety Guards**: Multi-layer circuit breakers and data sentinels to halt trading on stale data or risk breaches.

---
**Engineering Excellence for Data-Driven Trading.**
<!-- generated-by: gsd-doc-writer -->
