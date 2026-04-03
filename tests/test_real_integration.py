import pytest
import pandas as pd
from pathlib import Path
from ui.backtest_pages.single_test import load_backtest_data
from backtest.signal_generator import generate_signals

def test_real_csv_integration():
    """
    V-Model Level 3: Integration Testing with Real Files.
    Tests the full pipeline from raw disk CSV to signal generation.
    """
    # 1. 鎖定之前出錯的真實檔案
    real_file_path = Path("logs/market_data/TMF_20260401_PAPER_indicators.csv")
    
    if not real_file_path.exists():
        pytest.skip(f"Real data file {real_file_path} not found for integration test.")

    # 2. 模擬 Dashboard 載入流程 (測試 load_backtest_data)
    # 此函數現在包含：格式轉換、欄位去重、指標自癒重算
    try:
        df = load_backtest_data("specific", "20260401")
        
        assert df is not None
        assert not df.empty
        assert isinstance(df.index, pd.DatetimeIndex)
        assert "Close" in df.columns
        assert "ema_filter" in df.columns
        assert not df.columns.duplicated().any(), "Duplicate columns found after processing!"
        
    except Exception as e:
        pytest.fail(f"Integration Phase 1 (Data Loading) failed: {str(e)}")

    # 3. 模擬策略執行流程 (測試 generate_signals)
    cfg = {"strategy": {"regime_filter": "mid", "entry_score": 20}}
    try:
        longs, shorts = generate_signals(df, "squeeze_breakout", cfg)
        assert len(longs) == len(df)
        print(f"Successfully generated {longs.sum()} buy signals and {shorts.sum()} sell signals from real data.")
        
    except Exception as e:
        pytest.fail(f"Integration Phase 2 (Signal Generation) failed: {str(e)}")

def test_q1_dataset_robustness():
    """驗證 Q1 大型原始資料集的處理能力"""
    real_q1_path = Path("data/taifex_raw/TMF_5m_taifex.csv")
    if not real_q1_path.exists():
        pytest.skip("Q1 raw data not found.")
        
    df = load_backtest_data("q1")
    assert "ema_filter" in df.columns, "Should automatically calculate indicators for raw Q1 data"
    assert len(df) > 1000
