import pytest
from types import SimpleNamespace
import pandas as pd
from core.strategy_context import StrategyContext
from core.strategy_registry import StrategyRegistry

def test_all_strategies_comply_with_eval_contract():
    """
    Contract 2: Every strategy.on_bar() must set self._last_eval (last_eval property).
    Contract 4: Strategy skips must have 'SKIP:' prefix (enforced by router, but check plugin defaults).
    """
    registry = StrategyRegistry()
    registry.discover()
    
    # Mock context
    bar = {
        "Close": 42000, "High": 42100, "Low": 41900, "Open": 42000,
        "atr": 50, "vwap": 42050, "adx": 25, "score": 0,
        "regime": "CHOP", "bias": "NEUTRAL", "router_bias": "NEUTRAL",
        "mom_state": 0, "mom_velo": 0, "volume_spike": 1.0,
        "timestamp": pd.Timestamp("2026-05-08 10:00:00")
    }
    
    context = StrategyContext(
        market=SimpleNamespace(
            last_bar=bar, 
            df_5m=pd.DataFrame([bar]), 
            df_15m=pd.DataFrame([bar]),
            df_1h=pd.DataFrame([bar]),
            regime="CHOP", 
            bias="NEUTRAL"
        ),
        position=SimpleNamespace(size=0, entry_price=0, unrealized_pnl=0, current_stop_loss=None),
        config={}
    )

    for meta in registry.list_all():
        name = meta["name"]
        strategy = registry.get(name)
        # Skip clearing state if it doesn't have it
        if hasattr(strategy, "_last_eval"):
            strategy._last_eval = None
            
        strategy.init(context)
        strategy.on_bar(context)
        
        # Contract 2: Must not be None
        assert strategy.last_eval is not None, f"Strategy {name} violated eval contract: last_eval is None after on_bar"
        
        # Contract 4 check: If not triggered, should have a skip_reason
        if not strategy.last_eval.triggered:
            assert strategy.last_eval.skip_reason is not None, f"Strategy {name} skipped without reason"
