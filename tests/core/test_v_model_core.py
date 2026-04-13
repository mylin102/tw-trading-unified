"""
V-Model Verification: core/data_manager.py and core/data_enricher.py
Verifies Parquet storage integrity and requirement-driven enrichment.
"""
import pytest
import pandas as pd
import numpy as np
from pathlib import Path
import shutil
from core.data_manager import DataManager
from core.data_enricher import DataEnricher

def test_data_manager_parquet_lifecycle():
    test_path = Path("data/test_historical")
    shutil.rmtree(test_path, ignore_errors=True)
    dm = DataManager(base_path=str(test_path))
    
    # 1. Save
    df = pd.DataFrame({"Close": [100, 101]}, index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
    dm.save_historical("TEST_TICKER", df)
    
    # 2. Check path
    assert dm.get_path("TEST_TICKER").suffix == ".parquet"
    assert dm.get_path("TEST_TICKER").exists()
    
    # 3. Load
    loaded = dm.load_historical("TEST_TICKER")
    assert len(loaded) == 2
    assert loaded["Close"].iloc[0] == 100
    
    # 4. Inventory
    inv = dm.get_inventory()
    assert "TEST.TICKER" in inv
    assert inv["TEST.TICKER"]["rows"] == 2
    
    shutil.rmtree(test_path, ignore_errors=True)

def test_data_enricher_dynamic_logic():
    enricher = DataEnricher()
    # Create dummy OHLCV
    df = pd.DataFrame({
        "Open": np.random.rand(100),
        "High": np.random.rand(100) + 1,
        "Low": np.random.rand(100) - 1,
        "Close": np.random.rand(100),
        "Volume": np.random.rand(100) * 1000
    }, index=pd.date_range("2026-01-01", periods=100, freq="5min"))
    
    # 1. Test ATR enrichment
    res = enricher.enrich(df, ["atr"])
    assert "atr" in res.columns
    assert not res["atr"].isna().all()
    
    # 2. Test Multiple (ATR + VWAP)
    res_multi = enricher.enrich(df, ["atr", "vwap"])
    assert "atr" in res_multi.columns
    assert "vwap" in res_multi.columns
    
    # 3. Test Unknown indicator (should log warning but not crash)
    res_unknown = enricher.enrich(df, ["invalid_ind"])
    assert len(res_unknown.columns) == 5 # Original count
