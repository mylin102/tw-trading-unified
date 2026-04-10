"""
Tests for Pattern Recognition Engine.
Includes a Geometric Pattern Generator for synthetic data testing.
"""
import pytest
import pandas as pd
import numpy as np
from strategies.stocks.pattern_engine import detect_cup_with_handle

def generate_synthetic_cup(
    length=200, 
    cup_depth=0.25, 
    handle_depth=0.10, 
    handle_length=30,
    base_price=100.0
):
    """
    Generates a synthetic price series forming a Cup with Handle.
    """
    # 0. Initial Peak (Left Edge)
    initial_peak = np.linspace(base_price * 0.9, base_price, 30)
    
    # 1. Left side of cup (descending)
    cup_length = length - handle_length - 35
    left_side_len = cup_length // 2
    right_side_len = cup_length - left_side_len
    
    # Left side: parabolic descent
    left_side = base_price * (1 - cup_depth * (np.linspace(0, 1, left_side_len)**2))
    
    # 2. Right side of cup (ascending) - recovery
    # MUST recover to 98% of base_price to provide a clean Right Lip
    right_side = base_price * (1 - cup_depth) + (base_price * 0.98 - base_price * (1 - cup_depth)) * (np.linspace(0, 1, right_side_len)**0.5)
    
    # Peak Stabilization
    peak_stable = np.linspace(right_side[-1], right_side[-1], 5)
    
    # 3. Handle (dip then flat)
    handle_peak = peak_stable[-1]
    handle_bottom = handle_peak * (1 - handle_depth)
    handle_dip = np.linspace(handle_peak, handle_bottom, 10) 
    handle_flat = np.linspace(handle_bottom, handle_bottom + 0.1, handle_length - 10)
    
    prices = np.concatenate([initial_peak, left_side, right_side, peak_stable, handle_dip, handle_flat])
    
    # Create DataFrame
    dates = pd.date_range(end=pd.Timestamp.now(), periods=len(prices), freq='D')
    df = pd.DataFrame({
        "Open": prices * 0.99,
        "High": prices * 1.01,
        "Low": prices * 0.98,
        "Close": prices,
        "Volume": np.random.randint(1000, 5000, size=len(prices))
    }, index=dates)
    
    return df

def test_detect_cup_with_handle_synthetic():
    """Verify that the engine detects a perfectly formed synthetic cup."""
    df = generate_synthetic_cup(length=200, cup_depth=0.25, handle_depth=0.10)
    
    result = detect_cup_with_handle(df, cup_depth_min=0.10, cup_depth_max=0.40, order=5)
    
    assert result["status"] is True, f"Failed: {result.get('reason')}"
    assert result["type"] == "cup_with_handle"
    assert 0.20 <= result["depth"] <= 0.30
    assert result["pivot_price"] > 0

def test_detect_cup_with_handle_too_deep():
    """Verify that a cup that is too deep is rejected."""
    df = generate_synthetic_cup(length=200, cup_depth=0.50) # 50% depth
    
    result = detect_cup_with_handle(df, cup_depth_max=0.35, order=5)
    
    assert result["status"] is False
    assert "invalid_cup_depth" in result["reason"]

def test_detect_cup_with_handle_handle_too_deep():
    """Verify that a handle that is too deep is rejected."""
    # Generate a VALID cup with 10% handle
    df = generate_synthetic_cup(length=200, handle_depth=0.10) 
    
    # Pass a strict 5% max handle depth -> should fail with handle_too_deep
    result = detect_cup_with_handle(df, handle_depth_max=0.05, order=5)
    
    assert result["status"] is False
    assert "handle_too_deep" in result["reason"]

if __name__ == "__main__":
    pytest.main([__file__])
