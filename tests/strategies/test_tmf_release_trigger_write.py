"""The trigger must not write leg state before broker confirmation.

The strategy now emits only a decision; monitor/client owns broker submission
and fill-confirmed state.  This test is intentionally updated from the old
pre-submit state-write contract.
"""
from unittest.mock import patch

from core.strategy_context import StrategyContext, MarketData, PositionView
from strategies.plugins.futures.active.mts_lifecycle_adapter import TrailGroupStatus
from tests.strategies.test_tmf_spread_atr import _make_bar, _setup_armed


def _capture_trigger_write(tmp_path, release_leg="far"):
    s, config = _setup_armed(tmp_path, release_stop_points=20,
                             confirm_ticks=0)
    s._released_leg = None  # helper artifact; isolate the trigger
    _bar_kw = {"far_close": 45800} if release_leg == "far" else \
        {"near_close": 45900, "far_close": 46000}
    bar = _make_bar(**_bar_kw)
    ctx = StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=2), config=config)
    calls = {}
    def _cap(**kw):
        calls.update(kw)
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state",
               side_effect=_cap), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"):
        s.on_bar(ctx)
    return s, calls


def test_trigger_write_keeps_both_legs_open_far(tmp_path):
    s, calls = _capture_trigger_write(tmp_path, "far")
    assert calls == {}
    assert s._released_leg is None
    # no trailing while the release is pending (PendingSubmit, no fill)
    assert s._lifecycle_oca.trail_group.status == TrailGroupStatus.INACTIVE


def test_trigger_write_keeps_both_legs_open_near(tmp_path):
    s, calls = _capture_trigger_write(tmp_path, "near")
    assert calls == {}
    assert s._released_leg is None
    assert s._lifecycle_oca.trail_group.status == TrailGroupStatus.INACTIVE
