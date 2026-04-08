import pytest
import pandas as pd
import numpy as np
from strategies.stocks.entry_strategies import strategy_it_window_dressing

def test_it_buy_signal_optimized():
    """V-Model Unit Test P0: 驗證投信連買優化邏輯 (2/5 Hits)"""
    df_5m = pd.DataFrame({
        'Close': np.random.uniform(100, 110, 65),
        'ma20': np.linspace(90, 94, 65),
        'ma60': np.linspace(80, 84, 65)
    })
    last_5m = df_5m.iloc[-1].to_dict()
    last_5m["it_buy_rolling_count"] = 2
    state = {"last_5m": last_5m, "df_5m": df_5m}

    result = strategy_it_window_dressing(state, {})
    assert result is not None
    assert result["action"] == "BUY"
    assert "IT_3DAY_BUY" in result["reason"]

def test_it_no_signal_low_momentum():
    """V-Model Unit Test P0: 驗證動能不足時不進場 (1/5 Hits)"""
    df_5m = pd.DataFrame({'Close': 100, 'ma20': 95, 'ma60': 85}, index=range(65))
    last_5m = df_5m.iloc[-1].to_dict()
    last_5m["it_buy_rolling_count"] = 1
    state = {"last_5m": last_5m, "df_5m": df_5m}

    result = strategy_it_window_dressing(state, {})
    assert result is None

def test_it_no_signal_below_ma20():
    """V-Model Unit Test P1: 驗證均線過濾邏輯"""
    df_5m = pd.DataFrame({
        'Close': np.linspace(85, 89, 65),
        'ma20': 95,
        'ma60': 85
    })
    last_5m = df_5m.iloc[-1].to_dict()
    last_5m["it_buy_rolling_count"] = 3
    state = {"last_5m": last_5m, "df_5m": df_5m}

    result = strategy_it_window_dressing(state, {})
    assert result is None
