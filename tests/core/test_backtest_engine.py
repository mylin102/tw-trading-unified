"""
Unit tests for core/backtest_engine.py
Verifies PnL, fees, and margin calculations for Stocks and Futures.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from core.backtest_engine import BacktestEngine, AssetProfile, AssetType, BacktestResult
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.signal import Signal

class DummyStrategy(StrategyBase):
    """Simple strategy that buys at bar 20 and exits at bar 40."""
    @property
    def name(self): return "dummy"
    
    @property
    def metadata(self): return {"asset_class": "futures"}
    
    def init(self, context): pass
    
    def on_bar(self, context: StrategyContext):
        if context.bar_counter == 20:
            return Signal(action="BUY", reason="entry", stop_loss=0.0)
        if context.bar_counter == 40:
            return Signal(action="EXIT", reason="exit", stop_loss=0.0)
        return None

def test_futures_backtest_math():
    # Setup dummy data: 100 bars, price goes from 10000 to 10100
    dates = [datetime(2026, 1, 1) + timedelta(minutes=5*i) for i in range(100)]
    prices = np.linspace(10000, 10100, 100)
    df = pd.DataFrame({"Close": prices}, index=dates)
    
    profile = AssetProfile(
        asset_type=AssetType.FUTURES,
        point_value=200,
        margin_per_lot=100000,
        fee_rate=0.0,
        tax_rate=0.0,
        min_fee=0.0
    )
    
    engine = BacktestEngine(profile=profile, initial_capital=1_000_000)
    strategy = DummyStrategy()
    
    result = engine.run(df, strategy)
    
    # Check trades
    assert len(result.trades) == 2
    entry = result.trades.iloc[0]
    exit = result.trades.iloc[1]
    
    # PnL = (prices[40] - prices[20]) * 200
    expected_pnl = (prices[40] - prices[20]) * 200
    assert exit["pnl"] == pytest.approx(expected_pnl)
    assert engine.cash == pytest.approx(1_000_000 + expected_pnl)

def test_stock_backtest_math():
    # Setup dummy data
    dates = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(100)]
    prices = np.linspace(100, 110, 100)
    df = pd.DataFrame({"Close": prices}, index=dates)
    
    profile = AssetProfile(
        asset_type=AssetType.STOCK,
        point_value=1,
        margin_per_lot=0,
        fee_rate=0.0,
        tax_rate=0.0,
        min_fee=0.0
    )
    
    engine = BacktestEngine(profile=profile, initial_capital=100_000)
    strategy = DummyStrategy()
    
    result = engine.run(df, strategy)
    
    # PnL = (prices[40] - prices[20]) * 1 share
    expected_pnl = (prices[40] - prices[20])
    assert result.metrics["total_pnl"] == pytest.approx(expected_pnl)
    assert engine.cash == pytest.approx(100_000 + expected_pnl)
