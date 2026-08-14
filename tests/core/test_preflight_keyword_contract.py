"""Shioaji positional-arg reliability: live broker api calls must use the
keyword form (account=account).  Shioaji's C-extension dispatches positional
calls unreliably (observed: list_positions(account) silently returned empty),
so the preflight flat judgment was untrustworthy.  A keyword-only fake api
locks the contract: positional would raise TypeError.
"""
from types import SimpleNamespace

import pytest

from core.live_broker_preflight import _safe_positions


class _KeywordOnlyApi:
    """Fake api that accepts ONLY keyword arguments (positional raises)."""

    def list_positions(self, *, account):
        if account != "A-1":
            raise AssertionError("wrong account")
        return [SimpleNamespace(code="TMFI6", direction="Sell",
                                quantity=1, avg_price=46016.0,
                                price=46016.0, last_price=45426.0)]

    def list_trades(self, *, account):
        return []

    def margin(self, *, account):
        return SimpleNamespace(available_margin=123456.0)

    def trading_limits(self, *, account):
        return SimpleNamespace()


def test_safe_positions_uses_keyword_account_call():
    rows = _safe_positions(_KeywordOnlyApi(), "A-1")
    # Positional call would raise TypeError; keyword must return the row.
    assert len(rows) == 1
    assert rows[0]["code"] == "TMFI6"
    assert rows[0]["direction"] == "sell"
    assert rows[0]["qty"] == 1
