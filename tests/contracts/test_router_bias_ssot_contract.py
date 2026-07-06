import pytest
import pandas as pd
from types import SimpleNamespace
from core.futures_strategy_router import route_futures_signal, FuturesBarRegimeResult
from core.strategy_context import StrategyContext

def test_router_bias_ssot_contract():
    """
    Contract 3: bar["router_bias"] must be present and equal bar["bias"].
    """
    # Mock registry
    class MockRegistry:
        def get(self, name): return None
    
    # Mock context with a raw bar
    raw_bar = {"Close": 42000, "bias": "OLD_BIAS"}
    
    context = StrategyContext(
        market=SimpleNamespace(
            last_bar=raw_bar,
            df_5m=pd.DataFrame([raw_bar]),
            df_15m=None,
            timestamp=pd.Timestamp("2026-05-08 10:00:00"),
            regime="WEAK"
        ),
        position=SimpleNamespace(size=0, entry_price=0, unrealized_pnl=0, current_stop_loss=None),
        config={}
    )
    
    regime_result = FuturesBarRegimeResult(
        regime="WEAK",
        bias="SHORT",
        confidence=1.0,
        reasons=["test"]
    )
    
    decision = route_futures_signal(
        registry=MockRegistry(),
        context=context,
        regime_result=regime_result,
        active_strategy_name=None
    )
    
    # Verify SSOT Injection
    assert "router_bias" in raw_bar, "router_bias missing from bar dict"
    assert raw_bar["router_bias"] == "SHORT", f"router_bias incorrect: {raw_bar['router_bias']}"
    assert raw_bar["bias"] == raw_bar["router_bias"], "bias does not match router_bias"
    assert raw_bar["router_regime"] == "WEAK"
