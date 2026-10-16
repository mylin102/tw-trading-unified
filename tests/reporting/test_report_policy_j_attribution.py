# 2026-08-04 review item 4 regression tests:
# resolve_exit_reason recognizes POLICY_J_TRIGGERED / COMBINED_EXIT_SUBMITTED
# as EXPLICIT_EVENT (Level 1b) and prefers the event's own exit_reason.
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.generate_daily_report import resolve_exit_reason


def test_policy_j_triggered_resolves_explicit():
    events = [{"event": "POLICY_J_TRIGGERED", "peak": 378.0, "current": 298.0, "giveback": 50.0}]
    reason, source = resolve_exit_reason({}, events)
    assert reason == "Policy J (COMBINED_EXIT)"
    assert source == "EXPLICIT_EVENT"


def test_combined_exit_submitted_resolves_explicit():
    events = [{"event": "COMBINED_EXIT_SUBMITTED", "strategy": "MTS_EXIT"}]
    reason, source = resolve_exit_reason({}, events)
    assert reason == "Policy J (COMBINED_EXIT)"
    assert source == "EXPLICIT_EVENT"


def test_policy_j_event_prefers_own_exit_reason():
    events = [{"event": "COMBINED_EXIT_SUBMITTED", "exit_reason": "COMBINED_TRAIL_GIVEBACK"}]
    reason, source = resolve_exit_reason({}, events)
    assert reason == "COMBINED_TRAIL_GIVEBACK"  # event's own reason wins
    assert source == "EXPLICIT_EVENT"


def test_no_policy_events_still_insufficient():
    reason, source = resolve_exit_reason({}, [])
    assert reason == "UNKNOWN"
    assert source == "INSUFFICIENT_EVIDENCE"
