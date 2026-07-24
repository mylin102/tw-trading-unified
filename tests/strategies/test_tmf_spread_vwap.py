"""
Unit tests for Adaptive VWAP Exit trailing stop tightening logic.
2026-07-08 Gemini CLI
"""
import pytest
import datetime
from unittest.mock import MagicMock, patch

from strategies.plugins.futures.active.tmf_spread import TMFSpread, Leg, Side, PositionPhase
from core.strategy_context import StrategyContext, MarketData, PositionView

class TestVWAPExit:
    @pytest.fixture
    def s(self):
        s = TMFSpread()
        s._restore_position_state = MagicMock(return_value=False)
        with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
            s.init(StrategyContext(
                market=MarketData(last_bar={}, ticker="TMF"),
                position=PositionView(size=0),
                config={
                    "params": {
                        "vwap_exit": {
                            "enabled": True,
                            "tighten_ratio": 0.3
                        },
                        "atr_multiplier_stop": 1.0,
                        "atr_multiplier_trail": 2.0,
                        "release_stop_points": 999.0,
                        "trail_distance_points": 999.0
                    }
                },
            ))
        return s

    def test_vwap_exit_disabled_does_not_tighten(self, s):
        s._params["vwap_exit"]["enabled"] = False
        
        # Setup position as single leg remaining: FAR leg LONG
        s._has_position = True
        s._side = "LONG"
        s._released_leg = "near"
        s._far_entry = 46000.0
        s._trail_dist_fixed = 50.0
        
        # Setup trailing group status
        s._lifecycle_oca.phase = PositionPhase.SINGLE_LEG
        s._lifecycle_oca.release_group.filled_leg = Leg.NEAR
        s._lifecycle_oca.trail_group.status = 1  # ARMED/ACTIVE
        
        # Price is 46050, Peak is 46100, far_vwap is 46080 (Price 46050 is below far_vwap 46080 -> violated!)
        s._peak = 46100.0
        s._nadir = 46000.0
        bar = {
            "atr": 20.0,  # trail_dist = 2.0 * 20.0 = 40.0
            "far_close": 46050.0,
            "far_vwap": 46080.0,
            "far_high": 46050.0,
            "far_low": 46050.0
        }
        
        # Since vwap_exit is disabled, trail_dist should be full (40.0).
        # Since price (46050) is higher than peak - trail_dist (46100 - 40 = 46060 is > 46050 -> wait, peak - trail_dist = 46060.
        # If price is 46050, it is <= 46060, so it would exit!)
        # Let's set far_close = 46070. Peak - 40.0 = 46060. Price 46070 > 46060, so it should NOT exit!
        bar["far_close"] = 46075.0
        bar["far_high"] = 46075.0
        bar["far_low"] = 46075.0
        
        with patch("strategies.plugins.futures.active.tmf_spread.evaluate_lifecycle_actions") as mock_eval:
            s._manage_position(46075.0, 46075.0, 0.0, datetime.datetime.now(), bar)
            assert mock_eval.called
            # Check the context passed to evaluate_lifecycle_actions
            ctx_passed = mock_eval.call_args[0][0]
            assert ctx_passed.trail_dist == 40.0  # untouched!

    def test_vwap_exit_enabled_tightens_when_violated(self, s):
        s._params["vwap_exit"]["enabled"] = True
        s._params["vwap_exit"]["tighten_ratio"] = 0.3
        
        # Setup position as single leg remaining: FAR leg LONG
        s._has_position = True
        s._side = "LONG"
        s._released_leg = "near"
        s._far_entry = 46000.0
        
        s._lifecycle_oca.phase = PositionPhase.SINGLE_LEG
        s._lifecycle_oca.release_group.filled_leg = Leg.NEAR
        s._lifecycle_oca.trail_group.status = 1
        
        s._peak = 46100.0
        s._nadir = 46000.0
        bar = {
            "atr": 20.0,  # default trail_dist = 2.0 * 20.0 = 40.0
            "far_close": 46075.0,
            "far_vwap": 46080.0,  # Price 46075 < VWAP 46080 -> violated!
            "far_high": 46075.0,
            "far_low": 46075.0
        }
        
        with patch("strategies.plugins.futures.active.tmf_spread.evaluate_lifecycle_actions") as mock_eval:
            s._manage_position(46075.0, 46075.0, 0.0, datetime.datetime.now(), bar)
            ctx_passed = mock_eval.call_args[0][0]
            # Tightened trail_dist = 40.0 * 0.3 = 12.0!
            assert ctx_passed.trail_dist == 12.0
