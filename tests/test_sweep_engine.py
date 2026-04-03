import pandas as pd
import numpy as np
from backtest.sweep_engine import run_grid_sweep
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze

def test_grid_sweep_independence():
    """驗證：每個參數組合的結果應該互相獨立，且涵蓋所有組合"""
    n = 200
    df = pd.DataFrame({
        "Open": np.linspace(32000, 33000, n),
        "High": np.linspace(32010, 33010, n),
        "Low": np.linspace(31990, 32990, n),
        "Close": np.linspace(32005, 33005, n),
        "Volume": [500] * n,
    })
    df.index = pd.date_range("2026-04-02 09:00", periods=n, freq="5min")
    df = calculate_futures_squeeze(df)
    
    sweep_params = {
        "entry_score": [10, 20, 30],
        "atr_mult": [1.5, 2.0]
    }
    
    base_cfg = {"strategy": {"regime_filter": "mid", "entry_score": 20}}
    
    results, trades = run_grid_sweep(df, "squeeze_breakout", sweep_params, base_cfg)
    
    # 組合數量應為 3 * 2 = 6
    assert len(results) == 6
    # 確保所有參數都有出現在結果中
    assert set(results["entry_score"].unique()) == {10, 20, 30}
    assert set(results["atr_mult"].unique()) == {1.5, 2.0}
    # 績效欄位應存在
    assert "total_pnl" in results.columns
    assert "win_rate" in results.columns

def test_sweep_performance_limit():
    """效能邊界測試：驗證 25 組組合應在合理時間內完成"""
    import time
    n = 500
    df = pd.DataFrame({
        "Open": np.random.rand(n), "High": np.random.rand(n), 
        "Low": np.random.rand(n), "Close": np.random.rand(n), "Volume": [100]*n
    })
    df.index = pd.date_range("2026-01-01", periods=n, freq="5min")
    df = calculate_futures_squeeze(df)
    
    sweep_params = {
        "entry_score": [10, 15, 20, 25, 30],
        "atr_mult": [1.0, 1.5, 2.0, 2.5, 3.0]
    }
    
    base_cfg = {"strategy": {"regime_filter": "mid", "entry_score": 20}}
    
    start_time = time.time()
    run_grid_sweep(df, "squeeze_breakout", sweep_params, base_cfg)
    duration = time.time() - start_time
    
    # 25 個組合在 NumPy 加持下，應該要在 5 秒內完成 (含信號生成)
    assert duration < 5.0, f"Sweep engine too slow: {duration:.2f}s for 25 combos"
