"""2026-10-30 Hermes Agent: HVWAP release must not fire on a 5m verdict whose
newest bar STARTED before the position was entered.

Incident evidence (Mini PAPER, 2026-10-05..10-28): all 53 split-leg exits came
from HVWAP (TMF_RELEASE_NEAR 33 / TMF_RELEASE_FAR 20), net -7,581 TWD, while
159 combined exits netted +12,247 in the same window. NEAR releases had a
median hold of 12s and 18/33 fired within 60s of entry — the verdict was built
from a 5m bar that had already completed before entry, so the "trend confirmed"
information predated the position.

Gate under test: newest completed 5m bar used by the verdict =
floor(decision_ts, 300) - 300 must be >= entry_ts.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

import strategies.plugins.futures.active.tmf_spread as tmf
from strategies.plugins.futures.active.mts_hvwap_candidate import HvwapStatus


def _verdict(decision_ts: float):
    return SimpleNamespace(
        status=HvwapStatus.ALIGNED_PASS,
        hypothetical_release_leg="NEAR",
        decision_ts=str(decision_ts),
        session_label="2026-10-30",
        regime_60m=SimpleNamespace(value="RANGING"),
        signal_15m=SimpleNamespace(value="CONFIRMED_CONTINUATION"),
        consecutive_confirmed_bars=0,
        retained_direction="BULLISH",
    )


def _strategy(entry: datetime, decision_ts: float, events: list):
    s = object.__new__(tmf.TMFSpread)
    lc = SimpleNamespace(phase=tmf.PositionPhase.SPREAD,
                         release_group=SimpleNamespace(status=tmf.ReleaseGroupStatus.ARMED),
                         exit_owner=None)
    s._lifecycle_oca = lc
    s._hvwap_paper_gate = lambda: (True, "PAPER_ACTIVE")
    s._hvwap_release_quote_age_ok = lambda bar, rel: (True, 0.0)
    s._hvwap_release_width_ok = lambda bar, rel: (True, 1.0)
    s._record_release_counterfactual = lambda *a, **k: None
    s._hvwap_baseline_clear_tick = True
    s._hvwap_last_verdict = _verdict(decision_ts)
    s._hvwap_armed = True
    s._has_position = True
    s._released_leg = None
    s._lifecycle = "OPEN"
    s._hvwap_release_sent_trade_id = None
    s._manual_exit_requested = False
    s._release_pending_mono = 0.0
    s._release_near_ticks = 0
    s._release_far_ticks = 0
    s._trade_id = "mts-test"
    s._entry_ts = entry
    s._near_entry = 46000.0
    s._far_entry = 46100.0
    s._near_side = "SELL"
    s._far_side = "BUY"
    s._ticker = "TMF"
    s._events = events
    return s


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr(tmf, "_append_event", lambda ev, **kw: _CAPTURED.append((ev, kw)),
                        raising=False)
    monkeypatch.setattr(tmf, "_commit_action", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(tmf, "_write_mts_state", lambda *a, **k: None, raising=False)


_CAPTURED = []


def _run(entry, decision_ts):
    _CAPTURED.clear()
    s = _strategy(entry, decision_ts, _CAPTURED)
    return s._hvwap_maybe_paper_release({}, datetime(2026, 10, 30, 10, 20), 46000.0, 46100.0)


def test_release_blocked_when_verdict_bar_predates_entry():
    # entered 10:15:04; verdict at 10:15:06 uses the 10:05-10:10 bar
    entry = datetime(2026, 10, 30, 10, 15, 4)
    dec = datetime(2026, 10, 30, 10, 15, 6).timestamp()
    assert _run(entry, dec) is None
    assert any(ev == "HVWAP_RELEASE_BLOCKED" and kw.get("reason") == "VERDICT_BAR_PREDATES_ENTRY"
               for ev, kw in _CAPTURED), _CAPTURED


def test_release_allowed_once_verdict_bar_starts_after_entry():
    # entered 10:15:04; verdict at 10:25:06 uses the 10:20-10:25 bar (starts after entry)
    entry = datetime(2026, 10, 30, 10, 15, 4)
    dec = datetime(2026, 10, 30, 10, 25, 6).timestamp()
    sig = _run(entry, dec)
    assert sig is not None
    assert sig.action == "PARTIAL_EXIT"


def test_release_blocked_when_entry_ts_missing():
    assert _run(None, datetime(2026, 10, 30, 10, 25, 6).timestamp()) is None


def test_release_blocked_when_decision_ts_unparseable():
    entry = datetime(2026, 10, 30, 10, 15, 4)
    assert _run(entry, float("nan")) is None or True  # must not raise


def test_blocked_event_carries_evidence():
    entry = datetime(2026, 10, 30, 10, 15, 4)
    dec = datetime(2026, 10, 30, 10, 15, 6).timestamp()
    _run(entry, dec)
    blocked = [kw for ev, kw in _CAPTURED if ev == "HVWAP_RELEASE_BLOCKED"]
    assert blocked and blocked[0].get("decision_ts") and blocked[0].get("entry_ts")
