"""State telemetry mirror: the strategy's entry state write must carry the
spread-dynamics fields (dz / spread_slope / velocity_ema / spread) so the
Dashboard can show them.  The SQL is authoritative; the state mirrors it
for display only.
"""
from unittest.mock import patch

from core.strategy_context import StrategyContext, MarketData, PositionView
from tests.strategies.test_tmf_spread_atr import _make_bar, _setup_armed


def test_entry_write_includes_dynamics(tmp_path):
    s, config = _setup_armed(tmp_path, release_stop_points=20,
                             confirm_ticks=0)
    # force the FLAT entry scenario (z above entry threshold)
    s._has_position = False
    s._lifecycle = "FLAT"
    s._broker_truth_flat = True  # skip restore; force the FLAT entry path
    bar = _make_bar(near_close=45850, far_close=46000, spread_z=3.0)
    bar["dz"] = -4.5
    bar["spread_slope"] = 0.12
    bar["velocity_ema"] = -0.15
    bar["spread"] = 150.0
    ctx = StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=0), config=config)
    calls = {}
    def _cap(**kw):
        calls.update(kw)
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state",
               side_effect=_cap), \
         patch("core.channel_safety.get_safety_state") as gs, \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"):
        gs.return_value.entry_allowed.return_value = True
        s.on_bar(ctx)
    assert calls.get("dz") == -4.5
    assert calls.get("spread_slope") == 0.12
    assert calls.get("velocity_ema") == -0.15
    assert calls.get("spread") == 150.0
