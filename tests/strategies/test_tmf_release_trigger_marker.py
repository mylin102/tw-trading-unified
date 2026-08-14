"""The release TRIGGER must not mark the leg released: released_leg is the
FILL-confirmation marker set only by sync_release /
_enter_single_leg_after_release_fill on LEG_FILLED.  Marking it at
submission hides the leg's inventory before the broker confirms the fill
and contradicts the release_group (filled_leg=None).  Until the fill, the
lifecycle stays RELEASE_PENDING (RELEASE_NEAR/FAR) with both legs shown.
"""
from unittest.mock import patch

from core.strategy_context import StrategyContext, MarketData, PositionView
from tests.strategies.test_tmf_spread_atr import _make_bar, _setup_armed


def test_release_trigger_keeps_released_leg_unset(tmp_path):
    s, config = _setup_armed(tmp_path, release_stop_points=20,
                              confirm_ticks=0)
    # _setup_armed leaves a stale released_leg='far' marker; reset it so
    # this test isolates the TRIGGER's behavior (it must not mark).
    s._released_leg = None
    # far leg LONG at 46000; far close 45800 -> PnL (45800-46000)*10 = -2000
    # <= -release_stop(20) -> far release decision fires
    bar = _make_bar(far_close=45800)
    ctx = StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=2), config=config)
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"):
        s.on_bar(ctx)
    # the trigger must NOT mark the leg released — fill confirmation only
    assert s._released_leg is None
    # lifecycle stays pending until the broker confirms the fill
    assert s._lifecycle in ("RELEASE_NEAR", "RELEASE_FAR")


def test_release_fill_sync_still_marks_released(tmp_path):
    """The fill-confirmed path must still set released_leg (regression
    guard: sync_release is the ONLY release marker)."""
    s, config = _setup_armed(tmp_path, release_stop_points=20)
    s._released_leg = None  # helper artifact; reset to isolate the sync
    assert s._released_leg is None
    s.sync_release(leg="far", price=45900.0, release_price=45800.0,
                   order_id="test-fill-1")
    assert s._released_leg == "far"
    assert "TRAILING" in s._lifecycle
