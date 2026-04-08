import pytest
import pandas as pd
import numpy as np
from strategies.stocks.entry_strategies import strategy_it_window_dressing

def test_it_3day_buy_signal():
    """
    V-Model Unit Test P0: 驗證投信連三買邏輯 (修正欄位名稱)
    """
    # 模擬 65 根 K 線數據，滿足 len(df_5m) < 60 檢查
    df_5m = pd.DataFrame({
        'Close': np.random.uniform(100, 110, 65),
        'ma20': np.linspace(90, 94, 65),
        'ma60': np.linspace(80, 84, 65)
    })
    
    # 最後一根 K 線注入連三買指標
    last_5m = df_5m.iloc[-1].to_dict()
    last_5m["it_buy_rolling_3_min"] = 500  # 代表過去三天投信最小買超 500 股
    
    result = strategy_it_window_dressing(last_5m, df_5m, {})
    assert result is not None
    assert result["action"] == "BUY"
    assert "IT_3DAY_BUY" in result["reason"]

def test_it_no_signal_when_chips_missing():
    """
    V-Model Unit Test P0: 驗證無籌碼數據或投信轉賣時不進場
    """
    df_5m = pd.DataFrame({'Close': np.random.uniform(100, 110, 65), 'ma20': 95, 'ma60': 85})
    
    # 案例 A: 籌碼欄位不存在
    result = strategy_it_window_dressing(df_5m.iloc[-1].to_dict(), df_5m, {})
    assert result is None
    
    # 案例 B: 投信最近三天有賣超紀錄 (min <= 0)
    last_5m = df_5m.iloc[-1].to_dict()
    last_5m["it_buy_rolling_3_min"] = -10
    result = strategy_it_window_dressing(last_5m, df_5m, {})
    assert result is None

def test_it_no_signal_below_ma20():
    """
    V-Model Unit Test P1: 驗證均線過濾邏輯
    """
    df_5m = pd.DataFrame({
        'Close': np.linspace(85, 89, 65), # 股價低於 ma20
        'ma20': 95,
        'ma60': 85
    })
    last_5m = df_5m.iloc[-1].to_dict()
    last_5m["it_buy_rolling_3_min"] = 500
    
    result = strategy_it_window_dressing(last_5m, df_5m, {})
    assert result is None
