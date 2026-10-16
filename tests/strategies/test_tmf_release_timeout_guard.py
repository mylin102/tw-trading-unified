"""MTS_RELEASE_TIMEOUT must not reset the lifecycle to OPEN while the
broker still holds the release order/position (watchdog-proof).  A reset
would drop stop/trail protection and permit a resend.  Broker-live ->
keep the pending lifecycle; only a broker-flat proof permits the reset.
"""
import time
from unittest.mock import patch

import pytest

from core.strategy_context import StrategyContext, MarketData, PositionView
from tests.strategies.test_tmf_spread_atr import _make_bar, _setup_armed


def _force_pending_release(s, lifecycle="RELEASE_FAR"):
    s._lifecycle = lifecycle
    s._release_mono = time.monotonic() - 120.0  # stuck > 60s
    s._broker_truth_flat = False  # broker still holds order/position


@pytest.mark.parametrize("lifecycle", ["RELEASE_NEAR", "RELEASE_FAR"])
def test_timeout_keeps_pending_when_broker_live(lifecycle, tmp_path):
    s, config = _setup_armed(tmp_path)
    _force_pending_release(s, lifecycle)
    bar = _make_bar()
    ctx = StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=2), config=config)
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"):
        result = s.on_bar(ctx)
    assert s._lifecycle == lifecycle  # NOT reset to OPEN
    assert result is None  # no resend


def test_timeout_resets_when_broker_flat(tmp_path):
    s, config = _setup_armed(tmp_path)
    _force_pending_release(s)
    s._broker_truth_flat = True  # broker proved flat -> reset allowed
    bar = _make_bar()
    ctx = StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=2), config=config)
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"):
        s.on_bar(ctx)
    assert s._lifecycle == "OPEN"
