"""The trigger's state write must NOT mark the release before the broker
confirms the fill: far_status/near_status stay OPEN, no realized override
is persisted, and released_leg is absent from the write.  The RELEASED
status / realized / released_leg are FILL-confirmation-only (sync_release
on LEG_FILLED) — otherwise the Dashboard hides the second leg while the
broker still shows a pending release order.
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
    assert calls.get("action") == "RELEASE_FAR"
    assert calls.get("far_status") == "OPEN"       # NOT RELEASED
    assert calls.get("near_status") == "OPEN"
    assert calls.get("far_realized_override") is None
    assert calls.get("near_realized_override") is None
    assert "released_leg" not in calls             # marker is fill-only
    assert s._released_leg is None
    # no trailing while the release is pending (PendingSubmit, no fill)
    assert s._lifecycle_oca.trail_group.status == TrailGroupStatus.INACTIVE


def test_trigger_write_keeps_both_legs_open_near(tmp_path):
    s, calls = _capture_trigger_write(tmp_path, "near")
    assert calls.get("action") == "RELEASE_NEAR"
    assert calls.get("far_status") == "OPEN"
    assert calls.get("near_status") == "OPEN"
    assert calls.get("far_realized_override") is None
    assert calls.get("near_realized_override") is None
    assert "released_leg" not in calls
    assert s._released_leg is None
    assert s._lifecycle_oca.trail_group.status == TrailGroupStatus.INACTIVE
