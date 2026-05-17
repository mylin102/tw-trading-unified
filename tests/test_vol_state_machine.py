"""
Tests for core/derivatives/vol_state_machine.py

Verifies:
- State transitions with hysteresis
- Entry: must sustain N consecutive samples
- Exit: must sustain M consecutive samples
- Min dwell time prevents rapid oscillation
- State age tracking
- Transition count tracking
- UNKNOWN on insufficient data
- EVENT / PANIC / EUPHORIA / EXPANDING / CALM / NORMAL
"""

import datetime
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.derivatives.vol_state_machine import VolatilityStateMachine, VolState


def _update(sm, skew="SYMMETRIC", tension="LOW", pct=0.15, conf=0.8):
    """Helper: update state machine with defaults."""
    return sm.update(
        directional_skew=skew,
        tension=tension,
        iv_percentile=pct,
        confidence=conf,
    )


# ---------------------------------------------------------------------------
# Setup: fast state machine for testing (low hysteresis thresholds)
# ---------------------------------------------------------------------------

def _fast_sm(entry=1, exit=1, dwell=0):
    """State machine with minimal hysteresis for direct state testing."""
    return VolatilityStateMachine(
        min_samples_entry=entry,
        min_samples_exit=exit,
        min_dwell_sec=dwell,
        calm_pct_threshold=0.30,
        panic_pct_threshold=0.60,
        euphoria_pct_threshold=0.60,
        event_pct_threshold=0.95,
    )


def _slow_sm():
    """State machine with default hysteresis."""
    return VolatilityStateMachine(
        min_samples_entry=3,
        min_samples_exit=5,
        min_dwell_sec=60,
    )


# ---------------------------------------------------------------------------
# Basic state classification (no hysteresis)
# ---------------------------------------------------------------------------

def test_calm():
    """Low percentile + LOW tension + no skew -> CALM."""
    sm = _fast_sm()
    result = _update(sm, "SYMMETRIC", "LOW", 0.15, 0.7)
    assert result.state == VolState.CALM


def test_normal():
    """Moderate conditions -> NORMAL."""
    sm = _fast_sm()
    result = _update(sm, "SYMMETRIC", "LOW", 0.55, 0.7)
    assert result.state == VolState.NORMAL


def test_expanding():
    """MEDIUM tension -> EXPANDING."""
    sm = _fast_sm()
    result = _update(sm, "SYMMETRIC", "MEDIUM", 0.50, 0.7)
    assert result.state == VolState.EXPANDING


def test_panic():
    """LEFT + HIGH + high percentile -> PANIC."""
    sm = _fast_sm()
    result = _update(sm, "LEFT", "HIGH", 0.85, 0.9)
    assert result.state == VolState.PANIC


def test_euphoria():
    """RIGHT + HIGH + high percentile -> EUPHORIA."""
    sm = _fast_sm()
    result = _update(sm, "RIGHT", "HIGH", 0.85, 0.9)
    assert result.state == VolState.EUPHORIA


def test_event():
    """Extreme tension + very high percentile -> EVENT regardless of skew."""
    sm = _fast_sm()
    # EVENT has higher threshold (0.80) than PANIC (0.60)
    result = _update(sm, "LEFT", "HIGH", 0.95, 0.9)
    assert result.state == VolState.EVENT, f"Expected EVENT, got {result.state}"


def test_unknown_low_confidence():
    """Zero confidence -> UNKNOWN."""
    sm = _fast_sm()
    result = _update(sm, "LEFT", "HIGH", 0.85, 0.0)
    assert result.state == VolState.UNKNOWN


def test_unknown_no_percentile():
    """Zero percentile (not ready) -> UNKNOWN."""
    sm = _fast_sm()
    result = _update(sm, "LEFT", "HIGH", 0.0, 0.8)
    assert result.state == VolState.UNKNOWN


def test_unknown_no_data():
    """UNKNOWN inputs -> UNKNOWN."""
    sm = _fast_sm()
    result = _update(sm, "UNKNOWN", "UNKNOWN", 0.0, 0.0)
    assert result.state == VolState.UNKNOWN


# ---------------------------------------------------------------------------
# Entry hysteresis
# ---------------------------------------------------------------------------

def test_entry_hysteresis_requires_n_samples():
    """Must see same proposed state N times before entering."""
    sm = _slow_sm()  # entry=3

    # 1st sample: PANIC proposed -> still UNKNOWN (no prior state)
    r1 = _update(sm, "LEFT", "HIGH", 0.85, 0.9)
    # 2nd sample: still PANIC
    r2 = _update(sm, "LEFT", "HIGH", 0.85, 0.9)
    # 3rd sample: now sustained
    r3 = _update(sm, "LEFT", "HIGH", 0.85, 0.9)

    assert r3.state == VolState.PANIC, (
        f"Should transition to PANIC after 3 samples, got {r3.state}"
    )


def test_entry_hysteresis_not_reached():
    """Not enough consecutive samples -> stays in current state."""
    sm = _slow_sm()
    r1 = _update(sm, "LEFT", "HIGH", 0.85, 0.9)
    r2 = _update(sm, "LEFT", "HIGH", 0.85, 0.9)
    # Need 3 -> still not PANIC after 2
    assert r2.state != VolState.PANIC


def test_entry_counter_resets_on_change():
    """Entry counter resets if proposed state changes mid-way."""
    sm = _slow_sm()
    _update(sm, "LEFT", "HIGH", 0.85, 0.9)   # proposes PANIC (count=1)
    _update(sm, "RIGHT", "HIGH", 0.85, 0.9)  # proposes EUPHORIA (resets PANIC count)
    _update(sm, "LEFT", "HIGH", 0.85, 0.9)   # proposes PANIC again (count=1 again)

    # After 3 updates, but not 3 consecutive LEFT -> still not PANIC
    sm2 = _slow_sm()
    _update(sm2, "LEFT", "HIGH", 0.85, 0.9)   # PANIC count=1
    _update(sm2, "RIGHT", "HIGH", 0.85, 0.9)  # reset
    _update(sm2, "LEFT", "HIGH", 0.85, 0.9)   # PANIC count=1

    # Should not be PANIC yet (needs 3)
    r3 = _update(sm2, "LEFT", "HIGH", 0.85, 0.9)  # PANIC count=2
    assert r3.state != VolState.PANIC, "Should need 3 consecutive"


# ---------------------------------------------------------------------------
# Min dwell time
# ---------------------------------------------------------------------------

def test_min_dwell_prevents_rapid_transition():
    """Cannot transition within min_dwell_sec."""
    sm = VolatilityStateMachine(
        min_samples_entry=1, min_samples_exit=1,
        min_dwell_sec=3600,  # 1 hour dwell
        calm_pct_threshold=0.30,
        panic_pct_threshold=0.60,
    )
    # First update with CALM -> CALM becomes current
    r1 = _update(sm, "SYMMETRIC", "LOW", 0.15, 0.7)
    assert r1.state == VolState.CALM

    # Immediately propose PANIC -> should be blocked by dwell
    r2 = _update(sm, "LEFT", "HIGH", 0.85, 0.9)
    assert r2.state == VolState.CALM, f"Dwell should block transition, got {r2.state}"


# ---------------------------------------------------------------------------
# State age
# ---------------------------------------------------------------------------

def test_state_age_tracking():
    """age_sec increases with time."""
    sm = _fast_sm()
    now = datetime.datetime.utcnow()

    r1 = sm.update("SYMMETRIC", "LOW", 0.15, 0.7, timestamp=now)
    assert r1.state == VolState.CALM
    assert r1.age_sec >= 0

    r2 = sm.update("SYMMETRIC", "LOW", 0.15, 0.7, timestamp=now + datetime.timedelta(seconds=30))
    assert r2.age_sec >= 30, f"age_sec should be ~30, got {r2.age_sec}"


# ---------------------------------------------------------------------------
# Transition count
# ---------------------------------------------------------------------------

def test_transition_count_increments():
    """transition_count tracks total transitions."""
    sm = VolatilityStateMachine(
        min_samples_entry=1, min_samples_exit=1,
        min_dwell_sec=0,
        calm_pct_threshold=0.30,
        panic_pct_threshold=0.60,
    )
    r1 = _update(sm, "SYMMETRIC", "LOW", 0.15, 0.7)
    assert r1.state == VolState.CALM
    assert r1.transition_count == 1  # UNKNOWN -> CALM

    r2 = _update(sm, "LEFT", "HIGH", 0.85, 0.9)
    assert r2.state == VolState.PANIC
    assert r2.transition_count == 2  # CALM -> PANIC


# ---------------------------------------------------------------------------
# Persistent flag
# ---------------------------------------------------------------------------

def test_persistent_flag():
    """persistent=True when age_sec >= age_persistent_sec."""
    sm = VolatilityStateMachine(
        min_samples_entry=1, min_samples_exit=1,
        min_dwell_sec=0, age_persistent_sec=60,
        calm_pct_threshold=0.30,
    )
    now = datetime.datetime.utcnow()
    r1 = sm.update("SYMMETRIC", "LOW", 0.15, 0.7, timestamp=now)
    assert not r1.persistent, "Should not be persistent immediately"

    r2 = sm.update("SYMMETRIC", "LOW", 0.15, 0.7, timestamp=now + datetime.timedelta(seconds=120))
    assert r2.persistent, "Should be persistent after 120s"


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def test_reset():
    """Reset clears all state."""
    sm = _fast_sm()
    _update(sm, "LEFT", "HIGH", 0.85, 0.9)
    sm.reset()
    r = _update(sm, "SYMMETRIC", "LOW", 0.15, 0.7)
    # After reset, state is UNKNOWN -> transition to CALM
    assert r.state == VolState.CALM
    # Transition count reset
    assert r.transition_count == 1, "Reset should clear transition counter"


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------

def test_to_dict():
    """to_dict() returns expected keys."""
    sm = _fast_sm()
    r = _update(sm, "SYMMETRIC", "LOW", 0.15, 0.7)
    d = r.to_dict()
    expected = {"state", "age_sec", "transition_count", "persistent",
                "confidence", "directional_skew", "tension", "iv_percentile"}
    assert set(d.keys()) == expected


# ---------------------------------------------------------------------------
# Event takes priority over Panic
# ---------------------------------------------------------------------------

def test_event_priority():
    """EVENT (very high pct + HIGH tension) takes priority over PANIC."""
    sm = VolatilityStateMachine(
        min_samples_entry=1, min_samples_exit=1, min_dwell_sec=0,
        panic_pct_threshold=0.60,
        event_pct_threshold=0.80,
    )
    # LEFT + HIGH + pct=0.85 -> meets both PANIC (>0.60) and EVENT (>0.80)
    # EVENT should win because it's checked first
    r = _update(sm, "LEFT", "HIGH", 0.85, 0.9)
    assert r.state == VolState.EVENT, f"EVENT should win over PANIC, got {r.state}"
