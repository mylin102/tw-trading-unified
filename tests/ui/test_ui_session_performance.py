# 2026-08-04 Gemini CLI: Unit tests for Day/Night session performance breakdown calculator
import pytest
from ui.attribution_dashboard import compute_session_breakdown


def test_compute_session_breakdown_all_realized():
    completed_trades = [
        # Day session trades
        {"trade_id": "T-001", "is_realized": True, "gross_pnl": 500.0, "entry_session": "DAY", "guard_period": "POST_GUARD"},
        {"trade_id": "T-002", "is_realized": True, "gross_pnl": -200.0, "entry_session": "DAY", "guard_period": "POST_GUARD"},
        # Night session trades
        {"trade_id": "T-003", "is_realized": True, "gross_pnl": 1200.0, "session": "NIGHT", "guard_period": "POST_GUARD"},
        {"trade_id": "T-004", "is_realized": True, "gross_pnl": 800.0, "session": "NIGHT", "guard_period": "POST_GUARD"},
    ]

    breakdown = compute_session_breakdown(completed_trades)

    # ALL
    assert breakdown["ALL"]["total_trades"] == 4
    assert breakdown["ALL"]["total_net"] == 2300.0
    assert breakdown["ALL"]["wins"] == 3
    assert breakdown["ALL"]["win_rate"] == 0.75

    # DAY
    assert breakdown["DAY"]["total_trades"] == 2
    assert breakdown["DAY"]["total_net"] == 300.0
    assert breakdown["DAY"]["wins"] == 1
    assert breakdown["DAY"]["win_rate"] == 0.5
    assert breakdown["DAY"]["profit_factor"] == 2.5  # 500 / 200

    # NIGHT
    assert breakdown["NIGHT"]["total_trades"] == 2
    assert breakdown["NIGHT"]["total_net"] == 2000.0
    assert breakdown["NIGHT"]["wins"] == 2
    assert breakdown["NIGHT"]["win_rate"] == 1.0
    assert breakdown["NIGHT"]["profit_factor"] == 99.9  # No losses -> max fallback


def test_compute_session_breakdown_empty_trades():
    breakdown = compute_session_breakdown([])

    assert breakdown["ALL"]["total_trades"] == 0
    assert breakdown["ALL"]["total_net"] == 0.0
    assert breakdown["ALL"]["win_rate"] == 0.0
    assert breakdown["ALL"]["profit_factor"] == 0.0

    assert breakdown["DAY"]["total_trades"] == 0
    assert breakdown["NIGHT"]["total_trades"] == 0


def test_compute_session_breakdown_pre_guard_filtering():
    completed_trades = [
        {"trade_id": "T-001", "is_realized": True, "gross_pnl": 1000.0, "entry_session": "DAY", "guard_period": "PRE_GUARD"},
        {"trade_id": "T-002", "is_realized": True, "gross_pnl": 300.0, "entry_session": "DAY", "guard_period": "POST_GUARD"},
    ]

    breakdown = compute_session_breakdown(completed_trades)

    assert breakdown["ALL"]["total_trades"] == 1
    assert breakdown["ALL"]["total_net"] == 300.0


def test_compute_session_breakdown_unrealized_filtering():
    completed_trades = [
        {"trade_id": "T-001", "is_realized": False, "gross_pnl": 500.0, "entry_session": "NIGHT", "guard_period": "POST_GUARD"},
        {"trade_id": "T-002", "is_realized": True, "gross_pnl": 400.0, "entry_session": "NIGHT", "guard_period": "POST_GUARD"},
    ]

    breakdown = compute_session_breakdown(completed_trades)

    assert breakdown["NIGHT"]["total_trades"] == 1
    assert breakdown["NIGHT"]["total_net"] == 400.0
