"""
Unit tests for core/signal_processing.py
Verifies Kalman Filter denoising and stability.
"""
import pytest
import pandas as pd
import numpy as np
from core.signal_processing import apply_kalman_filter

def test_kalman_denoising():
    # 1. Create a noisy signal
    # Using a larger dataset and more stable trend to avoid initial bias
    np.random.seed(42)
    t = np.linspace(0, 50, 500)
    pure_signal = 100 + 0.5 * t # Linear trend
    noise = np.random.normal(0, 1.0, 500)
    noisy_signal = pd.Series(pure_signal + noise, name="Close")
    
    # 2. Apply Filter with balanced Q/R
    # Q=1e-3, R=1.0 means we trust the data moderately
    filtered = apply_kalman_filter(noisy_signal, q=1e-3, r=1.0)
    
    assert len(filtered) == len(noisy_signal)
    
    # 3. Verify Variance Reduction (skip first 50 bars for warm-up)
    warmup = 50
    noisy_diff = (noisy_signal.values - pure_signal)[warmup:]
    filtered_diff = (filtered.values - pure_signal)[warmup:]
    
    noisy_var = np.var(noisy_diff)
    filtered_var = np.var(filtered_diff)
    
    print(f"\nNoisy Var: {noisy_var:.4f}")
    print(f"Filtered Var: {filtered_var:.4f}")
    
    # Filtered variance should be significantly lower
    assert filtered_var < noisy_var
    
    # 4. Verify Tracking
    correlation = np.corrcoef(filtered.values[warmup:], pure_signal[warmup:])[0, 1]
    assert correlation > 0.95

def test_kalman_empty():
    s = pd.Series([], dtype=float)
    filtered = apply_kalman_filter(s)
    assert filtered.empty
