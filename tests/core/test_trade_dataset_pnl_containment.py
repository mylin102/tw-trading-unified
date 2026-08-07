"""P1-A containment tests (2026-08-07, codex exit-policy audit follow-up).

core.trade_dataset._kpi_realized_pnl() must exclude fills belonging to
known-corrupt ledger trades (CORRUPT_REALIZED_PNL_TRADES) from cumulative
KPI sums, while summing everything else normally.
"""
import pytest

from core.trade_dataset import (
    CORRUPT_REALIZED_PNL_TRADES,
    _kpi_realized_pnl,
)


def test_constant_contains_corrupt_trade():
    assert "mts-auto-222204-082" in CORRUPT_REALIZED_PNL_TRADES


def test_kpi_excludes_corrupt_trade():
    fills = [
        {"trade_id": "mts-auto-222204-082", "realized_pnl": -427698.6},
        {"trade_id": "t-normal-1", "realized_pnl": 100.0},
        {"trade_id": "t-normal-2", "realized_pnl": -50.0},
    ]
    assert _kpi_realized_pnl(fills) == pytest.approx(50.0)


def test_kpi_empty():
    assert _kpi_realized_pnl([]) == 0.0


def test_kpi_missing_pnl_field_ignored():
    fills = [
        {"trade_id": "t-normal-3", "realized_pnl": None},
        {"trade_id": "t-normal-4"},  # no field at all
        {"trade_id": "t-normal-5", "realized_pnl": 12.5},
    ]
    assert _kpi_realized_pnl(fills) == pytest.approx(12.5)
