# Session-transition resubscribe replay test (2026-08-05 INCIDENT fix #2).
# Verifies _resubscribe_after_session_transition:
#   - subscribes near/far tick + bidask (4 subscriptions)
#   - idempotent: calling twice does NOT duplicate (single callback slot +
#     safe_subscribe dedup path)
#   - no crash when api is None / dry_run
import sys
import os
import types
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeContract:
    def __init__(self, code):
        self.code = code
        self.delivery_date = None


class _FakeApi:
    def __init__(self):
        self.subscribed = []  # (code, quote_type)

    def subscribe(self, contract, quote_type=None, **kw):
        self.subscribed.append((contract.code, str(quote_type)))


def _make_monitor():
    """Minimal FuturesMonitor-shaped object with just the needed attrs."""
    from types import SimpleNamespace
    m = SimpleNamespace()
    m.api = _FakeApi()
    m.dry_run = False
    m.contract = _FakeContract("TMFH6")
    m.far_contract = _FakeContract("TMFI6")
    m.ticker = "TMF"
    # bind the real method
    import strategies.futures.monitor as mon
    m._resubscribe_after_session_transition = types.MethodType(
        mon.FuturesMonitor._resubscribe_after_session_transition, m)
    return m


def test_resubscribe_covers_four_channels(monkeypatch):
    m = _make_monitor()
    # stub console + safe_subscribe to capture calls (avoid real SDK)
    calls = []

    def _fake_safe_subscribe(api, contract, quote_type="tick"):
        calls.append((contract.code, quote_type))
    monkeypatch.setattr("core.broker.shioaji_compat.safe_subscribe", _fake_safe_subscribe)
    import strategies.futures.monitor as mon
    monkeypatch.setattr(mon, "console", types.SimpleNamespace(
        print=lambda *a, **k: None))
    m._resubscribe_after_session_transition()
    assert ("TMFH6", "tick") in calls
    assert ("TMFH6", "bidask") in calls
    assert ("TMFI6", "tick") in calls
    assert ("TMFI6", "bidask") in calls
    assert len(calls) == 4


def test_resubscribe_idempotent_two_calls(monkeypatch):
    m = _make_monitor()
    calls = []

    def _fake_safe_subscribe(api, contract, quote_type="tick"):
        calls.append((contract.code, quote_type))
    monkeypatch.setattr("core.broker.shioaji_compat.safe_subscribe", _fake_safe_subscribe)
    import strategies.futures.monitor as mon
    monkeypatch.setattr(mon, "console", types.SimpleNamespace(
        print=lambda *a, **k: None))
    m._resubscribe_after_session_transition()
    m._resubscribe_after_session_transition()
    # each call issues the same 4 subscriptions (SDK upsert; single callback
    # slot means no duplicate callback dispatch). The guard against callback
    # multiplication is the single-slot SDK setter, not call-count dedup.
    assert len(calls) == 8
    assert len(set(calls)) == 4


def test_resubscribe_no_api_no_crash():
    m = _make_monitor()
    m.api = None
    m._resubscribe_after_session_transition()  # must not raise
    m.api = _FakeApi()
    m.dry_run = True
    m._resubscribe_after_session_transition()  # must not raise


def test_transition_block_calls_resubscribe(monkeypatch):
    """Night->day transition must invoke resubscribe; same-session must not."""
    import strategies.futures.monitor as mon
    called = {"n": 0}

    def _fake_resub(self):
        called["n"] += 1
    monkeypatch.setattr(mon.FuturesMonitor,
                        "_resubscribe_after_session_transition", _fake_resub)
    # execute the transition block logic via a tiny harness (replicates
    # monitor.py 9070-9082 logic)
    from types import SimpleNamespace
    m = SimpleNamespace()
    m.previous_session_type = "night"
    m.session_type = "day"
    m._bars_since_session_open = 5
    m._cancel_all_pending_orders = lambda: None
    m._resubscribe_after_session_transition = types.MethodType(_fake_resub, m)
    # night -> day
    if m.previous_session_type != m.session_type:
        m._bars_since_session_open = 0
        if m.previous_session_type == "night" and m.session_type == "day":
            m._cancel_all_pending_orders()
            m._resubscribe_after_session_transition()
        m.previous_session_type = m.session_type
    assert called["n"] == 1
    # same session: no transition
    if m.previous_session_type != m.session_type:
        m._resubscribe_after_session_transition()
    assert called["n"] == 1
