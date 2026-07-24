"""
Unit tests for MTS MTF Mode Configuration, Caching, and Injection.
2026-07-14 Gemini CLI
"""
import pytest
import datetime
import time
import pandas as pd
from unittest.mock import MagicMock, patch

from strategies.futures.monitor import FuturesMonitor, MtfSnapshot

def make_monitor(tmp_path):
    # Create a config file
    cfg_file = tmp_path / "futures.yaml"
    cfg_file.write_text("""
mts:
  enabled: true
  strategy: tmf_spread
  mtf:
    mode: shadow
    max_age_sec: 10
""")
    dummy_api = type("A", (), {})()
    m = FuturesMonitor(api=dummy_api, config_path=str(cfg_file), dry_run=True)
    m.setup()
    return m

def test_invalid_mtf_mode_falls_back_to_disabled(tmp_path):
    m = make_monitor(tmp_path)
    # Set invalid mode
    m.cfg["mts"]["mtf"]["mode"] = "invalid_mode"
    assert m._get_mtf_mode() == "disabled"

def test_disabled_mode_does_not_calculate_mtf(tmp_path):
    m = make_monitor(tmp_path)
    m.cfg["mts"]["mtf"]["mode"] = "disabled"
    
    with patch("strategies.futures.monitor.calculate_mtf_alignment") as mock_calc:
        m._update_mtf_snapshot({"5m": pd.DataFrame(), "15m": pd.DataFrame()})
        assert not mock_calc.called
        assert m._current_mtf_snapshot.reason == "DISABLED"
        assert m._current_mtf_snapshot.score is None

def test_completed_5m_bar_updates_mtf_snapshot_once(tmp_path):
    m = make_monitor(tmp_path)
    m.cfg["mts"]["mtf"]["mode"] = "shadow"
    
    processed = {
        "5m": pd.DataFrame([{"mom_state": 1}]),
        "15m": pd.DataFrame([{"mom_state": 2}])
    }
    
    mock_res = {"score": 42.0, "components": {"5m": 1.0, "15m": 1.0}}
    with patch("strategies.futures.monitor.calculate_mtf_alignment", return_value=mock_res) as mock_calc:
        m._update_mtf_snapshot(processed)
        assert mock_calc.called
        snap = m._current_mtf_snapshot
        assert snap.valid is True
        assert snap.score == 42.0
        assert snap.components == {"5m": 1.0, "15m": 1.0}
        assert snap.reason == "OK"
        assert isinstance(snap.timestamp, datetime.datetime)

def test_true_zero_score_remains_valid(tmp_path):
    m = make_monitor(tmp_path)
    m.cfg["mts"]["mtf"]["mode"] = "shadow"
    
    processed = {
        "5m": pd.DataFrame([{"mom_state": 1}]),
        "15m": pd.DataFrame([{"mom_state": 2}])
    }
    mock_res = {"score": 0.0, "components": {}}
    with patch("strategies.futures.monitor.calculate_mtf_alignment", return_value=mock_res):
        m._update_mtf_snapshot(processed)
        snap = m._current_mtf_snapshot
        assert snap.valid is True
        assert snap.score == 0.0
        assert snap.reason == "OK"

def test_mts_tick_injects_latest_valid_snapshot(tmp_path):
    m = make_monitor(tmp_path)
    m.cfg["mts"]["mtf"]["mode"] = "shadow"
    m._current_mtf_snapshot = MtfSnapshot(
        score=25.0,
        timestamp=datetime.datetime.now(),
        valid=True,
        components={"5m": 1.0},
        reason="OK"
    )
    
    bar = {"near_close": 46000.0}
    m._inject_mtf_snapshot(bar)
    
    assert bar["mtf_score"] == 25.0
    assert bar["mtf_valid"] is True
    assert bar["mtf_mode"] == "shadow"
    assert bar["mtf_reason"] == "OK"
    assert bar["mtf_components"] == {"5m": 1.0}
    assert isinstance(bar["mtf_age_sec"], float)

def test_stale_snapshot_is_injected_as_invalid(tmp_path):
    m = make_monitor(tmp_path)
    m.cfg["mts"]["mtf"]["mode"] = "shadow"
    m.cfg["mts"]["mtf"]["max_age_sec"] = 5
    
    # 6 seconds ago
    ts = datetime.datetime.now() - datetime.timedelta(seconds=6)
    m._current_mtf_snapshot = MtfSnapshot(
        score=25.0,
        timestamp=ts,
        valid=True,
        components={"5m": 1.0},
        reason="OK"
    )
    
    bar = {"near_close": 46000.0}
    m._inject_mtf_snapshot(bar)
    
    assert bar["mtf_score"] is None
    assert bar["mtf_valid"] is False
    assert bar["mtf_age_sec"] > 5.0
    assert bar["mtf_reason"] == "STALE_OR_INVALID"
    assert bar["mtf_components"] == {}

def test_disabled_mode_injects_none_not_zero(tmp_path):
    m = make_monitor(tmp_path)
    m.cfg["mts"]["mtf"]["mode"] = "disabled"
    
    bar = {"near_close": 46000.0}
    m._inject_mtf_snapshot(bar)
    
    assert bar["mtf_score"] is None
    assert bar["mtf_valid"] is False
    assert bar["mtf_age_sec"] is None
    assert bar["mtf_mode"] == "disabled"
    assert bar["mtf_reason"] == "DISABLED"

def test_shadow_mode_does_not_change_mts_signal(tmp_path):
    m = make_monitor(tmp_path)
    m.cfg["mts"]["mtf"]["mode"] = "shadow"
    m.trader = MagicMock()
    m.trader.position = 0
    
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    strategy = TMFSpread()
    strategy._restore_position_state = MagicMock(return_value=False)
    strategy.on_bar = MagicMock(return_value=None)
    strategy.on_tick = MagicMock(return_value=None)
    strategy.write_state = MagicMock()
    m._registry.get = MagicMock(return_value=strategy)
    
    # Prepare a bar
    bar = {"near_close": 45000.0, "far_close": 45100.0, "ts": "2026-07-13 09:00:00"}
    
    state_file = tmp_path / "mts_position_state.json"
    with patch("strategies.futures.monitor._mts_position_state_path", return_value=state_file):
        with patch.object(m, "_inject_mtf_snapshot") as mock_inject:
            # Dry-run check or mock open hours
            with patch("strategies.futures.monitor.is_taifex_futures_market_open", return_value=True):
                with patch.object(m, "_mts_has_open_position_from_fills", return_value=False):
                    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
                        m._mts_tick(enriched_bar=bar)
                        assert mock_inject.called
                        # Ensure on_bar is called with context
                        assert strategy.on_bar.called
                        ctx = strategy.on_bar.call_args[0][0]
                        # No changes to lifecycle and signals are expected
                        assert ctx.config.get("mtf_mode") is None  # Config passed should be normal mts params

def test_mtf_calculation_failure_does_not_break_mts_tick(tmp_path):
    m = make_monitor(tmp_path)
    m.cfg["mts"]["mtf"]["mode"] = "shadow"
    
    # Set a valid snapshot first
    m._current_mtf_snapshot = MtfSnapshot(
        score=15.0,
        timestamp=datetime.datetime.now(),
        valid=True,
        components={"5m": 1.0},
        reason="OK"
    )
    
    processed = {
        "5m": pd.DataFrame([{"mom_state": 1}]),
        "15m": pd.DataFrame([{"mom_state": 2}])
    }
    
    with patch("strategies.futures.monitor.calculate_mtf_alignment", side_effect=ValueError("math error")):
        # This calculation should fail but not raise exception
        m._update_mtf_snapshot(processed)
        snap = m._current_mtf_snapshot
        
        # It should retain the previous snapshot values
        assert snap.score == 15.0
        assert snap.valid is True
        assert snap.reason == "CALC_FAILED"
