import pytest
import sys
from pathlib import Path

# Ensure project root is in path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def test_sweep_engine_imports():
    """驗證 sweep_engine 是否具備所有必要的公開介面"""
    try:
        from backtest.sweep_engine import run_portfolio_grid_sweep, run_multi_asset_backtest
        print("✅ sweep_engine imports OK")
    except ImportError as e:
        pytest.fail(f"Critical Import Error in sweep_engine: {e}")

def test_stock_optimizer_dependencies():
    """驗證 Stock Optimizer 頁面的所有依賴項"""
    try:
        from backtest.stock_engine import simulate_stock_trades
        from backtest.sweep_engine import run_multi_asset_backtest
        from backtest.signal_generator import generate_signals
        print("✅ Stock Optimizer dependencies OK")
    except ImportError as e:
        pytest.fail(f"Stock Optimizer is missing dependencies: {e}")
