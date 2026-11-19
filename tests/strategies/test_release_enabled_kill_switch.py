"""2026-11-19 Hermes Agent: quick-task 261119-ll7 — RELEASE_ENABLED kill switch.

User decision: keep MTS spread trading only via Policy J COMBINED_EXIT when
the operator wants it; RELEASE_ENABLED is a static module-level constant
(no #ifdef in Python, so a constant is the equivalent — deliberately NOT
wired to config/params/a flag file/dashboard; changing it requires a code
change + redeploy). When False it neutralizes BOTH known RELEASE-decision
sources in tmf_spread.py:

  1. HVWAP-candidate path: _hvwap_maybe_paper_release() (tmf_spread.py:2897)
  2. ATR/adapter path: `_decision = _adapter_res.decision` consumer in
     on_bar() (tmf_spread.py:5686)

Default True: behavior must be byte-identical to before this switch existed.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import strategies.plugins.futures.active.tmf_spread as tmf
from strategies.plugins.futures.active.mts_hvwap_candidate import HvwapStatus
from strategies.plugins.futures.active.tmf_spread import (
    Leg, LifecycleAction, LifecycleDecision, PositionLifecycle, PositionPhase,
    ReleaseGroup, ReleaseGroupStatus, TMFSpread,
)
from core.strategy_context import MarketData, PositionView, StrategyContext


# ── Part A: HVWAP-candidate path (the dominant real-world release source) ──
# Reuses the exact fixture shape from test_hvwap_release_entry_bar_guard.py
# (proven to reach a real PARTIAL_EXIT signal when the switch is True).

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


def _hvwap_strategy():
    s = object.__new__(TMFSpread)
    lc = SimpleNamespace(phase=PositionPhase.SPREAD,
                         release_group=SimpleNamespace(status=ReleaseGroupStatus.ARMED),
                         exit_owner=None)
    s._lifecycle_oca = lc
    s._hvwap_paper_gate = lambda: (True, "PAPER_ACTIVE")
    s._hvwap_release_quote_age_ok = lambda bar, rel: (True, 0.0)
    s._hvwap_release_width_ok = lambda bar, rel: (True, 1.0)
    s._record_release_counterfactual = lambda *a, **k: None
    s._hvwap_baseline_clear_tick = True
    # entered well before the verdict bar so the entry-bar guard passes
    entry = datetime(2026, 10, 30, 10, 15, 4)
    dec = datetime(2026, 10, 30, 10, 25, 6).timestamp()
    s._hvwap_last_verdict = _verdict(dec)
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
    return s


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr(tmf, "_append_event", lambda ev, **kw: None, raising=False)
    monkeypatch.setattr(tmf, "_commit_action", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(tmf, "_write_mts_state", lambda *a, **k: None, raising=False)


def test_hvwap_release_blocked_when_release_switch_off(monkeypatch):
    monkeypatch.setattr(tmf, "RELEASE_ENABLED", False)
    s = _hvwap_strategy()
    sig = s._hvwap_maybe_paper_release({}, datetime(2026, 10, 30, 10, 20), 46000.0, 46100.0)
    assert sig is None


def test_hvwap_release_allowed_when_release_switch_on(monkeypatch):
    monkeypatch.setattr(tmf, "RELEASE_ENABLED", True)
    s = _hvwap_strategy()
    sig = s._hvwap_maybe_paper_release({}, datetime(2026, 10, 30, 10, 20), 46000.0, 46100.0)
    assert sig is not None
    assert sig.action == "PARTIAL_EXIT"


def test_hvwap_release_allowed_at_module_default():
    # Does not touch the switch at all — proves the shipped default (True)
    # reproduces pre-switch production behavior with zero wiring changes.
    assert tmf.RELEASE_ENABLED is True
    s = _hvwap_strategy()
    sig = s._hvwap_maybe_paper_release({}, datetime(2026, 10, 30, 10, 20), 46000.0, 46100.0)
    assert sig is not None
    assert sig.action == "PARTIAL_EXIT"


# ── Part B: ATR/adapter path — neutralize a RELEASE decision from
# self._lifecycle_adapter.evaluate() before it reaches the dispatcher. ──

@pytest.fixture
def _strategy_for_adapter_path():
    s = TMFSpread()
    s._restore_position_state = MagicMock(return_value=False)
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        config = {
            "ticker": "TMF",
            "params": {
                "min_atr": 5.0,
                "trail_distance_points": 35.0,
                "atr_multiplier_stop": 1.0,
                "atr_multiplier_trail": 3.5,
                "confirm_ticks": 1,
                "confirm_ms": 0.0,
            },
        }
        s.init(StrategyContext(market=MarketData(last_bar={}, ticker="TMF"),
                                position=PositionView(size=0), config=config))
    return s


def test_adapter_release_decision_neutralized_when_switch_off(_strategy_for_adapter_path, monkeypatch):
    monkeypatch.setattr(tmf, "RELEASE_ENABLED", False)
    s = _strategy_for_adapter_path
    s._has_position = True
    s._near_side = "LONG"
    s._far_side = "SHORT"
    s._near_entry = 44000.0
    s._far_entry = 44100.0
    s._lifecycle = "OPEN"
    s._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    fake_adapter = MagicMock()
    fake_adapter.evaluate.return_value = SimpleNamespace(
        decision=LifecycleDecision(action=LifecycleAction.RELEASE,
                                    release_leg=Leg.NEAR, winner="TEST_FORCED_RELEASE"),
    )
    s._lifecycle_adapter = fake_adapter

    bar = {
        "near_close": 44000.0, "far_close": 44100.0, "atr": 10.0,
        "near_bid": 43999.0, "near_ask": 44001.0,
        "far_bid": 44099.0, "far_ask": 44101.0,
        "timestamp": datetime.now(),
    }
    ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                           position=PositionView(size=1), config={})
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._commit_action"):
        s.on_bar(ctx)

    # The forced adapter RELEASE must never have been committed: no
    # released_leg, lifecycle stays OPEN/SPREAD (not RELEASE_*).
    assert s._released_leg is None
    assert not str(getattr(s, "_lifecycle", "")).startswith("RELEASE_")
