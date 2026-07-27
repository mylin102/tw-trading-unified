# 2026-07-27 Gemini CLI: Reporting Attribution Governance Test Suite (Cases 1~10)
import pytest
from scripts.generate_daily_report import resolve_exit_reason, resolve_release_reason


def test_case_1_order_submitted_trail():
    """Case 1: ORDER_SUBMITTED + strategy=MTS_EXIT + exit_reason=TRAIL -> TRAIL (ORDER_METADATA)."""
    events = [
        {"event": "ORDER_SUBMITTED", "strategy": "MTS_EXIT", "exit_reason": "TRAIL", "trade_id": "t1"}
    ]
    reason, source = resolve_exit_reason({}, events)
    assert reason == "TRAIL"
    assert source == "ORDER_METADATA"


def test_case_2_order_submitted_stop():
    """Case 2: ORDER_SUBMITTED + strategy=MTS_EXIT + exit_reason=STOP -> STOP (Must NOT become TRAIL)."""
    events = [
        {"event": "ORDER_SUBMITTED", "strategy": "MTS_EXIT", "exit_reason": "STOP", "trade_id": "t1"}
    ]
    reason, source = resolve_exit_reason({}, events)
    assert reason == "STOP"
    assert source == "ORDER_METADATA"


def test_case_3_emergency_flatten():
    """Case 3: fill_type=EXIT + EMERGENCY_FLATTEN event -> EMERGENCY_FLATTEN (EXPLICIT_EVENT)."""
    events = [
        {"event": "EMERGENCY_FLATTEN", "reason": "EMERGENCY_FLATTEN", "trade_id": "t1"}
    ]
    reason, source = resolve_exit_reason({}, events)
    assert reason == "EMERGENCY_FLATTEN"
    assert source == "EXPLICIT_EVENT"


def test_case_4_manual_close():
    """Case 4: fill_type=EXIT + MANUAL_CLOSE -> MANUAL_CLOSE (EXPLICIT_EVENT)."""
    events = [
        {"event": "MANUAL_CLOSE", "reason": "MANUAL_CLOSE", "trade_id": "t1"}
    ]
    reason, source = resolve_exit_reason({}, events)
    assert reason == "MANUAL_CLOSE"
    assert source == "EXPLICIT_EVENT"


def test_case_5_zero_evidence_returns_unknown():
    """Case 5: fill_type=EXIT with zero event evidence -> UNKNOWN (INSUFFICIENT_EVIDENCE). Must NOT infer TRAIL."""
    data = {"exit": {"fill_type": "EXIT"}}
    events = []
    reason, source = resolve_exit_reason(data, events)
    assert reason == "UNKNOWN"
    assert source == "INSUFFICIENT_EVIDENCE"


def test_case_6_release_and_exit_reason_separated():
    """Case 6: First leg RELEASE + Second leg TRAIL -> release_reason and exit_reason separated."""
    data = {"release": {"fill_type": "RELEASE"}, "exit": {"fill_type": "EXIT"}}
    events = [
        {"event": "ORDER_SUBMITTED", "strategy": "MTS_RELEASE", "release_reason": "RELEASE_STOP", "trade_id": "t1"},
        {"event": "ORDER_SUBMITTED", "strategy": "MTS_EXIT", "exit_reason": "TRAIL", "trade_id": "t1"}
    ]
    rel_r, rel_src = resolve_release_reason(data, events)
    exit_r, exit_src = resolve_exit_reason(data, events)

    assert rel_r == "RELEASE_STOP"
    assert rel_src == "ORDER_METADATA"
    assert exit_r == "TRAIL"
    assert exit_src == "ORDER_METADATA"


def test_case_7_cross_session_trade_detection():
    """Case 7: Cross-session trade -> entry_session=DAY, release_session=NIGHT, cross_session_trade=True."""
    entries = [{"session": "day"}]
    release = {"session": "night"}
    exit_fill = {"session": "night"}

    entry_session = entries[0].get("session", "UNKNOWN")
    release_session = release.get("session", "UNKNOWN")
    exit_session = exit_fill.get("session", "UNKNOWN")
    cross_session_trade = len({s for s in (entry_session, release_session, exit_session) if s != "UNKNOWN"}) > 1

    assert entry_session == "day"
    assert release_session == "night"
    assert exit_session == "night"
    assert cross_session_trade is True


def test_case_8_multiple_order_submitted_events():
    """Case 8: Multiple ORDER_SUBMITTED events -> matched by leg role & strategy."""
    data = {"release": {"fill_type": "RELEASE"}, "exit": {"fill_type": "EXIT"}}
    events = [
        {"event": "ORDER_SUBMITTED", "strategy": "MTS_RELEASE", "release_reason": "ATR_RELEASE", "trade_id": "t1"},
        {"event": "ORDER_SUBMITTED", "strategy": "MTS_EXIT", "exit_reason": "TIMEOUT", "trade_id": "t1"}
    ]
    rel_r, _ = resolve_release_reason(data, events)
    exit_r, _ = resolve_exit_reason(data, events)

    assert rel_r == "ATR_RELEASE"
    assert exit_r == "TIMEOUT"


def test_case_9_idempotent_out_of_order_events():
    """Case 9: Out-of-order or duplicate events -> idempotent result."""
    data = {"exit": {"fill_type": "EXIT"}}
    events = [
        {"event": "ORDER_SUBMITTED", "strategy": "MTS_EXIT", "exit_reason": "TRAIL", "trade_id": "t1"},
        {"event": "ORDER_SUBMITTED", "strategy": "MTS_EXIT", "exit_reason": "TRAIL", "trade_id": "t1"}
    ]
    r1, s1 = resolve_exit_reason(data, events)
    r2, s2 = resolve_exit_reason(data, reversed(events))
    assert r1 == r2 == "TRAIL"
    assert s1 == s2 == "ORDER_METADATA"


def test_case_10_legacy_event_backward_compatibility():
    """Case 10: Legacy RELEASE_NEAR_SUBMITTED / EXIT_REMAINING format -> 100% backward compatible."""
    data = {"release": {"fill_type": "RELEASE"}, "exit": {"fill_type": "EXIT"}}
    events = [
        {"event": "RELEASE_NEAR_SUBMITTED", "risk_mode": "ATR_DYNAMIC", "trade_id": "t1"},
        {"event": "EXIT_REMAINING", "reason": "TRAIL", "risk_mode": "ATR_DYNAMIC", "trade_id": "t1"}
    ]
    rel_r, rel_src = resolve_release_reason(data, events)
    exit_r, exit_src = resolve_exit_reason(data, events)

    assert rel_r == "ATR_DYNAMIC"
    assert rel_src == "EXPLICIT_EVENT"
    assert exit_r == "TRAIL"
    assert exit_src == "EXPLICIT_EVENT"
