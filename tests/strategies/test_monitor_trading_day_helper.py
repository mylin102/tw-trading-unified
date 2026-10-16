"""The raw-kbar writer's trading-day lookup must actually resolve.

Live 2026-09-29 06:33 (startup): "Tick CSV rebuild failed (non-fatal): name
'get_trading_day_str' is not defined" — monitor.py called a helper that was
never imported, so the raw-kbar CSV truth source silently stopped being
written.  The canonical, already-imported helper is core.date_utils.
get_session_date_str, which also models the exchange rollover correctly
(night session 15:00-05:00 belongs to the NEXT trading day); the unimported
tick_writer duplicate rolled at 05:00 and disagreed with it.
"""
from datetime import datetime

import strategies.futures.monitor as monitor

SRC = __import__("pathlib").Path("strategies/futures/monitor.py").read_text(encoding="utf-8")


def test_monitor_does_not_reference_the_undefined_helper():
    assert "get_trading_day_str" not in SRC


def test_the_canonical_helper_is_wired_into_the_monitor_namespace():
    assert monitor.get_session_date_str(datetime(2026, 9, 29, 10, 0)) == "20260929"


def test_the_rollover_matches_the_exchange_convention():
    # the night session of T belongs to the next trading day
    assert monitor.get_session_date_str(datetime(2026, 9, 29, 16, 0)) == "20260930"
    assert monitor.get_session_date_str(datetime(2026, 9, 29, 4, 30)) == "20260929"
    assert monitor.get_session_date_str(datetime(2026, 9, 30, 4, 30)) == "20260930"
