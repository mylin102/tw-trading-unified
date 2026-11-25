"""2026-11-25 Hermes Agent: watchdog expiry check must not compare date to str.

During the 2026-11-25 network outage the process restarted 16 times. Root cause
in `FuturesMonitor._check_futures_contract_staleness`:

    today_str = datetime.now().strftime("%Y/%m/%d")          # str "2026/11/25"
    if self.contract and self.contract.delivery_date < today_str:   # date < str
        TypeError: '<' not supported between instances of 'datetime.date' and 'str'

`contract_resolver` parses delivery_date with strptime (legacy str), but the
contracts-v2 object used by `self.contract` carries a `datetime.date`. So the
light-recovery path crashed whenever the feed went stale — i.e. exactly during
an outage, when the watchdog matters most.
"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from strategies.futures.monitor import FuturesMonitor


def _monitor(delivery_date):
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.dry_run = False
    m.api = object()
    m.ticker = "TMF"
    m.contract = SimpleNamespace(code="TMFL6", delivery_date=delivery_date)
    m._running = True
    m.MONITOR = {"stale_tick_warn_secs": 120, "stale_tick_critical_secs": 600}
    # attributes touched before the expiry check (numeric, set per-run)
    m.feed_health = None
    m.market_data = {}
    m._far_current_bar = {}
    return m


def _run(m, age_secs):
    """Drive _check_futures_contract_staleness into the light-recovery band."""
    calls = {"rollover": 0}
    import time as _t
    _now = _t.time()
    m._last_real_tmf_tick_at = _now - age_secs
    m._last_tick_at = _now - age_secs
    m.last_tick_at = _now - age_secs

    def _rollover(*a, **kw):
        calls["rollover"] += 1

    with patch.object(FuturesMonitor, "_tmf_feed_age_secs", return_value=age_secs), \
         patch.object(FuturesMonitor, "_set_runtime_status", lambda *a, **k: None), \
         patch.object(FuturesMonitor, "_get_tick_bars_df", return_value=None), \
         patch.object(FuturesMonitor, "_check_contract_rollover", _rollover), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open", return_value=True), \
         patch("strategies.futures.monitor.console"):
        m._check_futures_contract_staleness()
    return calls


@pytest.mark.parametrize("age", [200.0, 300.0])
def test_date_delivery_date_does_not_raise(age):
    """contracts-v2 carries datetime.date — the expiry check must not TypeError."""
    m = _monitor(date(2026, 11, 30))          # future delivery
    calls = _run(m, age)                       # must not raise
    assert calls["rollover"] >= 1              # light recovery still attempted


def test_expired_date_still_triggers_rollover():
    """An actually-expired contract (date) must still be detected."""
    m = _monitor(date(2020, 1, 1))
    calls = _run(m, 200.0)
    assert calls["rollover"] >= 1


def test_legacy_str_delivery_date_still_works():
    """Legacy object uses a "YYYY/MM/DD" string; behaviour must be unchanged."""
    m = _monitor("2020/01/01")
    calls = _run(m, 200.0)
    assert calls["rollover"] >= 1


def test_unparseable_delivery_date_does_not_raise():
    """Garbage delivery_date must degrade to 'not expired', never raise."""
    m = _monitor("not-a-date")
    calls = _run(m, 200.0)
    assert calls["rollover"] >= 1
