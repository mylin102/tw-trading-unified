"""
Squeeze Futures Business Logic Module

業務邏輯模組：
- RiskManager: 風險管理（停損監控）
- SignalGenerator: 信號生成（開盤買進）
- CapitalManager: 資金控制
- PerformanceOptimizer: 速度優化

使用方式：
    from squeeze_futures.business import (
        RiskManager, RiskLimits,
        SignalGenerator, SignalConfig,
        CapitalManager, CapitalConfig,
        PerformanceOptimizer,
    )
"""

from .risk_manager import RiskManager, RiskLimits, PositionRisk
from .signal_generator import SignalGenerator, SignalConfig, Signal
from .capital_manager import CapitalManager, CapitalConfig, PositionSizing
from .performance_optimizer import (
    PerformanceOptimizer,
    PerformanceProfiler,
    MemoryPool,
    calculate_returns_vectorized,
    calculate_drawdown_vectorized,
    simulate_portfolio_vectorized,
    fast_cache,
)

__all__ = [
    # 風險管理
    'RiskManager',
    'RiskLimits',
    'PositionRisk',
    
    # 信號生成
    'SignalGenerator',
    'SignalConfig',
    'Signal',
    
    # 資金控制
    'CapitalManager',
    'CapitalConfig',
    'PositionSizing',
    
    # 速度優化
    'PerformanceOptimizer',
    'PerformanceProfiler',
    'MemoryPool',
    'calculate_returns_vectorized',
    'calculate_drawdown_vectorized',
    'simulate_portfolio_vectorized',
    'fast_cache',
]
