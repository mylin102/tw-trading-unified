
import pytest
import pandas as pd
from scripts.analysis.adaptive_analyzer import AdaptiveAnalyzer
from pathlib import Path

def test_correlate_trades_logic():
    """V-Cycle: Unit Test for trade-indicator correlation logic."""
    analyzer = AdaptiveAnalyzer("20260416")
    
    # Mock data
    trades = pd.DataFrame([
        {'timestamp': '2026-04-16 09:35:00', 'type': 'SELL', 'reason': 'CUM_DELTA'}
    ])
    trades['timestamp'] = pd.to_datetime(trades['timestamp'])
    
    indicators = pd.DataFrame([
        {'timestamp': '2026-04-16 09:34:55', 'score': 85.0, 'trend': 'BEARISH'},
        {'timestamp': '2026-04-16 09:35:05', 'score': 90.0, 'trend': 'BEARISH'}
    ])
    indicators['timestamp'] = pd.to_datetime(indicators['timestamp'])
    
    data = {'trades': trades, 'indicators': indicators}
    enriched = analyzer.correlate_trades(data)
    
    assert not enriched.empty
    # Direction 'backward' should pick the 09:34:55 indicator for 09:35:00 trade
    assert enriched.iloc[0]['score'] == 85.0
    assert enriched.iloc[0]['trend'] == 'BEARISH'

def test_analyze_reason_alpha_logic():
    """V-Cycle: Unit Test for alpha calculation logic."""
    analyzer = AdaptiveAnalyzer("20260416")
    
    enriched = pd.DataFrame([
        {'reason': 'CUM_DELTA', 'type': 'ENTRY', 'pnl_cash': 0},
        {'reason': 'CUM_DELTA', 'type': 'EXIT', 'pnl_cash': 500},
        {'reason': 'VWAP_BOUNCE', 'type': 'ENTRY', 'pnl_cash': 0},
        {'reason': 'VWAP_BOUNCE', 'type': 'EXIT', 'pnl_cash': -200}
    ])
    
    summary = analyzer.analyze_reason_alpha(enriched)
    
    assert 'CUM_DELTA' in summary
    assert summary['CUM_DELTA']['win_rate'] == 1.0
    assert summary['VWAP_BOUNCE']['win_rate'] == 0.0
