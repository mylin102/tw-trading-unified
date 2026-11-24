"""2026-11-24 Hermes Agent: sync_release must pass near_last/far_last so
_write_mts_state's realized-pnl branch actually runs for the released leg.

Before this fix, sync_release()'s _write_mts_state call omitted near_last/
far_last entirely (both default to 0 in the signature). _write_mts_state's
internal realized-pnl computation is gated on "<leg>_last > 0", so it never
executed -- near_realized_pnl/far_realized_pnl stayed 0.0 forever after a
real release, and the dashboard's "(已平倉)" badge (ui/dashboard.py:
`if _r: ...`) never fired for an already-released leg, showing stale live
UPL instead.
"""
from unittest.mock import patch

from tests.strategies.test_tmf_spread_atr import _setup_armed


def test_sync_release_passes_last_prices_for_realized_pnl_branch(tmp_path):
    s, _config = _setup_armed(tmp_path, release_stop_points=20)
    s._released_leg = None  # helper artifact; isolate this sync call

    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)

    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state",
               side_effect=_capture), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
        s.sync_release(leg="far", price=45900.0, release_price=45800.0,
                       order_id="test-fill-upl-1")

    # far released (fill price 45800) -> far_last must carry the release
    # price so the realized-pnl branch fires for it.
    assert captured.get("far_last") == 45800.0
    # near remains open at the live remaining-leg price passed to sync_release.
    assert captured.get("near_last") == 45900.0
    # Guard against the regression shape: both silently defaulting to 0.
    assert captured.get("far_last") != 0
    assert captured.get("near_last") != 0


def test_sync_release_near_released_maps_last_prices_correctly(tmp_path):
    s, _config = _setup_armed(tmp_path, release_stop_points=20)
    s._released_leg = None

    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)

    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state",
               side_effect=_capture), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
        s.sync_release(leg="near", price=46100.0, release_price=45650.0,
                       order_id="test-fill-upl-2")

    # near released (fill price 45650) -> near_last must carry the release
    # price; far remains open at the live remaining-leg price.
    assert captured.get("near_last") == 45650.0
    assert captured.get("far_last") == 46100.0
