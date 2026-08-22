"""Phase 1 completion tests (2026-08-22): freshness VALUE gate + session gate
on the tmf_spread._build_trend_release_input seam."""
import pytest

from strategies.plugins.futures.active.tmf_spread import TMFSpread


def _valid_snapshot(**over):
    snap = {
        "decision_ts": "2026-07-20T10:00:00",
        "asof_ts": "2026-07-20T10:00:00",
        "direction": "BULLISH",
        "confidence": 0.90,
        "pass_release": True,
        "decision_max_quote_age_ms": 50.0,
        "window_max_quote_age_ms": 200.0,
        "session": "DAY",
    }
    snap.update(over)
    return snap


def _strat(**over):
    s = object.__new__(TMFSpread)
    s._near_side = "LONG"
    s._far_side = "SHORT"
    s._position_session_type = "DAY"
    s._max_quote_age_ms = 1000.0
    s._trend_confirmed_snapshot = _valid_snapshot()
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_p1_fresh_and_valid_passes(monkeypatch):
    monkeypatch.setenv("MTS_TREND_RELEASE_ENABLED", "1")
    s = _strat()
    enabled, snap = s._build_trend_release_input()
    assert enabled is True
    assert snap is not None
    assert snap["release_leg"] == "FAR"


def test_p1_stale_decision_age_blocks(monkeypatch):
    monkeypatch.setenv("MTS_TREND_RELEASE_ENABLED", "1")
    s = _strat(_trend_confirmed_snapshot=_valid_snapshot(decision_max_quote_age_ms=5000.0))
    enabled, snap = s._build_trend_release_input()
    assert enabled is False and snap is None


def test_p1_stale_window_age_blocks(monkeypatch):
    monkeypatch.setenv("MTS_TREND_RELEASE_ENABLED", "1")
    s = _strat(_trend_confirmed_snapshot=_valid_snapshot(window_max_quote_age_ms=99999.0))
    enabled, snap = s._build_trend_release_input()
    assert enabled is False and snap is None


def test_p1_negative_age_blocks(monkeypatch):
    monkeypatch.setenv("MTS_TREND_RELEASE_ENABLED", "1")
    s = _strat(_trend_confirmed_snapshot=_valid_snapshot(decision_max_quote_age_ms=-1.0))
    enabled, snap = s._build_trend_release_input()
    assert enabled is False and snap is None


def test_p1_non_numeric_age_blocks(monkeypatch):
    monkeypatch.setenv("MTS_TREND_RELEASE_ENABLED", "1")
    s = _strat(_trend_confirmed_snapshot=_valid_snapshot(decision_max_quote_age_ms="stale"))
    enabled, snap = s._build_trend_release_input()
    assert enabled is False and snap is None


def test_p1_session_mismatch_blocks(monkeypatch):
    monkeypatch.setenv("MTS_TREND_RELEASE_ENABLED", "1")
    s = _strat(_trend_confirmed_snapshot=_valid_snapshot(session="NIGHT"),
               _position_session_type="DAY")
    enabled, snap = s._build_trend_release_input()
    assert enabled is False and snap is None


def test_p1_session_match_passes(monkeypatch):
    monkeypatch.setenv("MTS_TREND_RELEASE_ENABLED", "1")
    s = _strat(_trend_confirmed_snapshot=_valid_snapshot(session="NIGHT"),
               _position_session_type="NIGHT")
    enabled, snap = s._build_trend_release_input()
    assert enabled is True and snap is not None


def test_p1_env_off_blocks_even_with_valid_snapshot(monkeypatch):
    monkeypatch.delenv("MTS_TREND_RELEASE_ENABLED", raising=False)
    s = _strat()
    enabled, snap = s._build_trend_release_input()
    assert enabled is False and snap is None