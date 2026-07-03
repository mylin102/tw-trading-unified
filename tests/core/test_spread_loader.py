# 2026-06-26 Gemini CLI: unit tests for SpreadLoader fail-fast features
import pytest
import os
from core.spread_loader import SpreadLoader

def test_spread_loader_fail_fast_load_latest_csv():
    loader = SpreadLoader()
    
    # Passing empty string or None should raise ValueError
    with pytest.raises(ValueError, match="Ticker cannot be None or empty"):
        loader.load_latest_csv("")
        
    with pytest.raises(ValueError, match="Ticker cannot be None or empty"):
        loader.load_latest_csv(None)

def test_spread_loader_fail_fast_find_and_load():
    loader = SpreadLoader()
    
    # Calling internal _find_and_load with None should raise ValueError
    with pytest.raises(ValueError, match="Ticker must be explicitly provided"):
        loader._find_and_load("spread", None)
        
    with pytest.raises(ValueError, match="Ticker must be explicitly provided"):
        loader._find_and_load("spread", "")

def test_spread_loader_fail_fast_check_reload():
    loader = SpreadLoader()
    
    # Mock self._csv_paths to trigger a reload check
    loader._csv_paths["spread"] = __file__  # Use this test file as dummy path
    loader._spread_csv_mtime = 0.0  # Force mismatch to trigger reload
    
    # Since self._ticker is None, it should raise ValueError when attempting reload
    with pytest.raises(ValueError, match="Cannot hot-reload because no ticker is cached"):
        loader._check_reload()
