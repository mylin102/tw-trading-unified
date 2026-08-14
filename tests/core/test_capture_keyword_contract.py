"""Monitor capture Shioaji keyword contract: api calls must use keyword args
(account=account).  Shioaji positional dispatch silently returns empty (the
phantom-flat that let the restart through), so the canonical positions must
come from the keyword form.  Keyword-only fake api locks the contract.
"""
from types import SimpleNamespace

import pytest

from strategies.futures.monitor import FuturesMonitor


class _KeywordOnlyApi:
    """Fake api accepting ONLY keyword args (positional raises TypeError)."""

    @property
    def futopt_account(self):
        return SimpleNamespace(account_no="F-1")

    @property
    def stock_account(self):
        return None

    def list_positions(self, *, account):
        return [SimpleNamespace(code="TMFI6", direction="Sell", quantity=1,
                                price=46016.0, pnl=760.0)]

    def list_trades(self, *args, **kwargs):
        return []

    def margin(self, *args, **kwargs):
        return SimpleNamespace(available_margin=500000.0)


def _monitor():
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.api = _KeywordOnlyApi()
    mon.live_trading = True
    mon.dry_run = False
    mon._append_mts_event = lambda *a, **k: None
    return mon


def test_capture_positions_use_keyword_account_call():
    mon = _monitor()
    payload = mon._capture_post_startup_snapshot()
    fut = [p for p in (payload.get("positions") or [])
           if p.get("account") == "futures"]
    assert len(fut) == 1
    assert fut[0]["code"] == "TMFI6"
    assert fut[0]["direction"] == "Sell"
    assert fut[0]["quantity"] == 1
