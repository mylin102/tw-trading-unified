import pytest
import pandas as pd
from types import SimpleNamespace
from strategies.plugins.futures.active.weak_bear_trend import WeakBearTrend
from core.strategy_context import StrategyContext

def test_weak_bear_trend_contract_weak_short_enabled():
    """
    Contract 5: WEAK + SHORT 時 weak_bear_trend 不可再出現 BIAS_NOT_SHORT.
    """
    strategy = WeakBearTrend()
    
    # Mock bar with WEAK regime and SHORT bias
    bar = {
        "Close": 41900, "High": 41950, "Low": 41850,
        "vwap": 42000, "atr": 50, "adx": 30, "mom_velo": -10,
        "volume_spike": 1.1, "regime": "WEAK", "bias": "SHORT",
        "router_bias": "SHORT", "router_regime": "WEAK",
        "timestamp": pd.Timestamp("2026-05-08 10:00:00")
    }
    
    # Mock context
    context = StrategyContext(
        market=SimpleNamespace(
            last_bar=bar,
            df_5m=pd.DataFrame([bar]*10), # enough bars
            regime="WEAK",
            bias="SHORT"
        ),
        position=SimpleNamespace(size=0, entry_price=0, unrealized_pnl=0, current_stop_loss=None),
        config={"params": {"max_adx": 50.0, "shadow_mode": False}}
    )
    
    strategy.init(context)
    strategy.on_bar(context)
    
    # Check eval
    eval_result = strategy.last_eval
    assert eval_result.skip_reason != "BIAS_NOT_SHORT", "WeakBearTrend failed to recognize SHORT bias from router"
    # Should either trigger or have a different skip reason (e.g. mom_velo if set high)
    if not eval_result.triggered:
        assert eval_result.skip_reason != "BIAS_NOT_SHORT"
