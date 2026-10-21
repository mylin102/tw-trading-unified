# 2026-10-21 Hermes Agent: NET-only MTS session KPI and gross-only count contracts.
import pytest
from ui.attribution_dashboard import compute_session_breakdown


def test_compute_session_breakdown_all_realized():
    completed_trades = [
        # Day session trades
        {"trade_id": "T-001", "is_realized": True, "pnl_status": "NET", "net_pnl": 500.0, "gross_pnl": 550.0, "entry_session": "DAY", "guard_period": "POST_GUARD"},
        {"trade_id": "T-002", "is_realized": True, "pnl_status": "NET", "net_pnl": -200.0, "gross_pnl": -180.0, "entry_session": "DAY", "guard_period": "POST_GUARD"},
        # Night session trades
        {"trade_id": "T-003", "is_realized": True, "pnl_status": "NET", "net_pnl": 1200.0, "gross_pnl": 1250.0, "session": "NIGHT", "guard_period": "POST_GUARD"},
        {"trade_id": "T-004", "is_realized": True, "pnl_status": "NET", "net_pnl": 800.0, "gross_pnl": 810.0, "session": "NIGHT", "guard_period": "POST_GUARD"},
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
        {"trade_id": "T-001", "is_realized": True, "pnl_status": "NET", "net_pnl": 1000.0, "gross_pnl": 1050.0, "entry_session": "DAY", "guard_period": "PRE_GUARD"},
        {"trade_id": "T-002", "is_realized": True, "pnl_status": "NET", "net_pnl": 300.0, "gross_pnl": 320.0, "entry_session": "DAY", "guard_period": "POST_GUARD"},
    ]

    breakdown = compute_session_breakdown(completed_trades)

    assert breakdown["ALL"]["total_trades"] == 1
    assert breakdown["ALL"]["total_net"] == 300.0


def test_compute_session_breakdown_unrealized_filtering():
    completed_trades = [
        {"trade_id": "T-001", "is_realized": False, "pnl_status": "NET", "net_pnl": 500.0, "gross_pnl": 550.0, "entry_session": "NIGHT", "guard_period": "POST_GUARD"},
        {"trade_id": "T-002", "is_realized": True, "pnl_status": "NET", "net_pnl": 400.0, "gross_pnl": 420.0, "entry_session": "NIGHT", "guard_period": "POST_GUARD"},
    ]

    breakdown = compute_session_breakdown(completed_trades)

    assert breakdown["NIGHT"]["total_trades"] == 1
    assert breakdown["NIGHT"]["total_net"] == 400.0


def test_gross_only_trades_are_excluded_from_net_kpis_and_counted():
    completed_trades = [
        {"trade_id": "net-win", "is_realized": True, "pnl_status": "NET",
         "net_pnl": 70.0, "gross_pnl": 100.0, "entry_session": "DAY",
         "guard_period": "POST_GUARD"},
        {"trade_id": "net-loss", "is_realized": True, "pnl_status": "NET",
         "net_pnl": -40.0, "gross_pnl": -20.0, "entry_session": "NIGHT",
         "guard_period": "POST_GUARD"},
        {"trade_id": "legacy-gross", "is_realized": True,
         "pnl_status": "GROSS_ONLY", "net_pnl": None, "gross_pnl": 9000.0,
         "entry_session": "DAY", "guard_period": "POST_GUARD"},
    ]

    breakdown = compute_session_breakdown(completed_trades)

    assert breakdown["ALL"]["total_trades"] == 2
    assert breakdown["ALL"]["total_net"] == 30.0
    assert breakdown["ALL"]["wins"] == 1
    assert breakdown["ALL"]["win_rate"] == 0.5
    assert breakdown["ALL"]["profit_factor"] == 1.75
    assert breakdown["ALL"]["gross_only_count"] == 1
    assert breakdown["DAY"]["total_trades"] == 1
    assert breakdown["DAY"]["gross_only_count"] == 1
    assert breakdown["NIGHT"]["gross_only_count"] == 0
