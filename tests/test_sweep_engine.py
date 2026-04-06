import pandas as pd
import numpy as np
from backtest.sweep_engine import run_portfolio_grid_sweep
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze

def _make_stock_df(n=200, base=100):
    df = pd.DataFrame({
        "Open": np.linspace(base, base+10, n),
        "High": np.linspace(base+0.5, base+10.5, n),
        "Low": np.linspace(base-0.5, base+9.5, n),
        "Close": np.linspace(base+0.2, base+10.2, n),
        "Volume": [500] * n,
    })
    df.index = pd.date_range("2026-04-02 09:00", periods=n, freq="5min")
    return calculate_futures_squeeze(df)

def test_portfolio_grid_sweep_independence():
    """驗證：每個參數組合的結果應該互相獨立，且涵蓋所有組合"""
    all_dfs = {"TEST1": _make_stock_df(200, 100), "TEST2": _make_stock_df(200, 50)}
    
    sweep_params = {
        "stop_loss_pct": [0.02, 0.03, 0.05],
        "trailing_stop_pct": [0.01, 0.02]
    }
    base_cfg = {"strategy": {"entry_score": 20, "scout_strategy": {"atr_mult": 2.0}}}
    
    results = run_portfolio_grid_sweep(all_dfs, "scout_strategy", sweep_params, base_cfg)
    
    # 組合數量應為 3 * 2 = 6
    assert len(results) == 6
    assert set(results["stop_loss_pct"].unique()) == {0.02, 0.03, 0.05}
    assert set(results["trailing_stop_pct"].unique()) == {0.01, 0.02}
    # 績效欄位應存在
    assert "Total_PnL" in results.columns
    assert "Total_Trades" in results.columns
    assert "Profitable_Ratio" in results.columns

def test_sweep_performance_limit():
    """效能邊界測試：25 組組合應在合理時間內完成"""
    import time
    all_dfs = {f"T{i}": _make_stock_df(200, 50+i*10) for i in range(5)}
    
    sweep_params = {
        "stop_loss_pct": [0.02, 0.03, 0.04, 0.05, 0.06],
        "trailing_stop_pct": [0.01, 0.015, 0.02, 0.025, 0.03]
    }
    base_cfg = {"strategy": {"entry_score": 20, "scout_strategy": {"atr_mult": 2.0}}}
    
    start_time = time.time()
    run_portfolio_grid_sweep(all_dfs, "scout_strategy", sweep_params, base_cfg)
    duration = time.time() - start_time
    
    assert duration < 30.0, f"Sweep engine too slow: {duration:.2f}s for 25 combos x 5 assets"
