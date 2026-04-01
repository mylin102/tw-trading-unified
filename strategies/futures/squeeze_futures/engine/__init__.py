"""
Squeeze Futures Engine

核心交易引擎模組：
- DataManager: 數據管理
- VectorizedSimulator: 向量化模擬器
- QuantAnalytics: 量化分析
- indicators: 技術指標
- simulator: 傳統模擬器（向後相容）
- execution: 訂單執行
- constants: 常數定義
"""

from .data import DataManager
from .vectorized import VectorizedSimulator, SimulatorConfig
from .analytics import QuantAnalytics, TradeStats, RiskMetrics, PerformanceMetrics
from .indicators import calculate_futures_squeeze, calculate_mtf_alignment, calculate_atr
from .simulator import PaperTrader
from .execution import ExecutionModel, simulate_order_fill, build_execution_model
from .constants import get_point_value, POINT_VALUE_BY_TICKER

__all__ = [
    # 核心元件
    'DataManager',
    'VectorizedSimulator',
    'SimulatorConfig',
    'QuantAnalytics',
    
    # 數據類別
    'TradeStats',
    'RiskMetrics',
    'PerformanceMetrics',
    
    # 指標
    'calculate_futures_squeeze',
    'calculate_mtf_alignment',
    'calculate_atr',
    
    # 傳統模擬器（向後相容）
    'PaperTrader',
    
    # 執行
    'ExecutionModel',
    'simulate_order_fill',
    'build_execution_model',
    
    # 常數
    'get_point_value',
    'POINT_VALUE_BY_TICKER',
]