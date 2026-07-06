import pytest
import pandas as pd
import numpy as np
from backtest.signal_generator import generate_signals
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze

class TestSignalGeneratorVModel:
    """V-Model Level 1: Unit Testing for Data Robustness"""

    def _make_raw_ohlcv(self, n=100):
        """模擬最原始的 Taifex CSV 格式 (只有小寫或大寫 OHLCV，無指標)"""
        df = pd.DataFrame({
            "open": np.linspace(32000, 33000, n),
            "high": np.linspace(32010, 33010, n),
            "low": np.linspace(31990, 32990, n),
            "close": np.linspace(32005, 33005, n),
            "volume": [500] * n,
            "timestamp": pd.date_range("2026-04-02 09:00", periods=n, freq="5min")
        })
        return df

    def test_auto_indicator_calculation(self):
        """驗證：當輸入只有 OHLCV 時，系統應能自動重算指標並產生信號"""
        df_raw = self._make_raw_ohlcv()
        
        # 在 generate_signals 之前，模擬 ui/dashboard 的處理邏輯
        # 1. 標準化欄位
        df = df_raw.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        
        # 2. 自動補齊指標 (這就是之前漏掉的步驟)
        df_ready = calculate_futures_squeeze(df)
        
        cfg = {"strategy": {"regime_filter": "mid", "entry_score": 20}}
        
        # 執行信號生成
        try:
            longs, shorts = generate_signals(df_ready, "squeeze_breakout", cfg, warmup=60)
            assert len(longs) == len(df_ready)
            assert not (longs & shorts).any(), "Cannot have long and short at the same time"
        except KeyError as e:
            pytest.fail(f"V-Model Failure: Strategy missing required column {e}")

    def test_case_insensitivity(self):
        """驗證：大小寫混雜的欄位名稱不應導致崩潰"""
        df = self._make_raw_ohlcv()
        df["Close"] = df["close"] # 同時存在大小寫
        df = calculate_futures_squeeze(df)
        
        cfg = {"strategy": {"regime_filter": "mid", "entry_score": 20}}
        longs, _ = generate_signals(df, "squeeze_breakout", cfg, warmup=60)
        assert len(longs) == 100

def test_all_strategies_compliance():
    """驗證：signal_generator 必須能兼容 entry_strategies.py 中的所有策略"""
    from strategies.futures.entry_strategies import STRATEGIES
    import numpy as np
    
    n = 150
    df = pd.DataFrame({
        "Open": np.random.rand(n), "High": np.random.rand(n), 
        "Low": np.random.rand(n), "Close": np.random.rand(n), "Volume": [100]*n
    })
    df.index = pd.date_range("2026-04-02", periods=n, freq="5min")
    df = calculate_futures_squeeze(df)
    
    cfg = {"strategy": {"regime_filter": "mid", "entry_score": 20, "momentum_burst": {"min_zscore": 2.0, "atr_mult": 2.0}}}
    
    for strat_name in STRATEGIES.keys():
        try:
            generate_signals(df, strat_name, cfg, warmup=60)
        except Exception as e:
            pytest.fail(f"Strategy '{strat_name}' failed V-Model compliance: {str(e)}")
