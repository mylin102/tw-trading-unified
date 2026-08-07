"""T1-T8: reconciliation (design §2)."""
import pytest

from scripts.research.exit_attribution.reconcile import (
    CORRUPT_TRADES,
    STATUS_CORRUPT,
    STATUS_FEE_UNCERTAIN,
    STATUS_MISMATCH,
    STATUS_OK,
    STATUS_UNRECONCILED,
    expected_close_realized,
    reconcile_fill,
)


def test_t1_long_close_sign():
    # close SELL closes LONG: exit>entry -> gain
    exp = expected_close_realized(110.0, 100.0, 1, 10.0, "SELL")
    assert exp == pytest.approx(100.0)


def test_t2_short_close_sign():
    # close BUY closes SHORT: exit<entry -> gain
    exp = expected_close_realized(90.0, 100.0, 1, 10.0, "BUY")
    assert exp == pytest.approx(100.0)


def test_t3_qty_and_partial_multi_fill():
    # qty=3 aggregated slice
    exp = expected_close_realized(110.0, 100.0, 3, 10.0, "SELL")
    assert exp == pytest.approx(300.0)
    # partial slices: 2 lots from avg 100 + 1 lot from avg 102
    total = expected_close_realized(110.0, 100.0, 2, 10.0, "SELL") + \
        expected_close_realized(110.0, 102.0, 1, 10.0, "SELL")
    assert total == pytest.approx(280.0)


def test_t4_leg_qty_mismatch():
    res = reconcile_fill(
        {"trade_id": "t1", "side": "SELL", "qty": 5, "price": 110.0},
        100.0, 10.0, expected_closed_qty=3,
    )
    assert res.status == STATUS_MISMATCH
    assert "leg_qty_mismatch" in res.issue_flags


def test_t5_missing_position_side():
    res = reconcile_fill({"trade_id": "t1", "qty": 1, "price": 110.0}, 100.0, 10.0)
    assert res.status == STATUS_UNRECONCILED
    assert "missing_position_side" in res.issue_flags


def test_t6_corrupt_trade_kept_not_dropped():
    assert "mts-auto-222204-082" in CORRUPT_TRADES
    res = reconcile_fill(
        {"trade_id": "mts-auto-222204-082", "side": "SELL", "qty": 1, "price": 42765.0,
         "realized_pnl": -427698.6},
        42749.0, 10.0,
    )
    assert res.status == STATUS_CORRUPT
    assert "corrupt_realized_pnl" in res.issue_flags


def test_t7_fee_missing_fee_uncertain():
    res = reconcile_fill(
        {"trade_id": "t1", "side": "SELL", "qty": 1, "price": 110.0},
        100.0, 10.0, fee_source_available=False,
    )
    assert res.status == STATUS_FEE_UNCERTAIN
    assert "fee_missing" in res.issue_flags
    assert res.expected_realized == pytest.approx(100.0)  # gross still computed


def test_t8_single_status_plus_flags():
    # corrupt trade with ALSO missing side: status stays CORRUPT (one axis),
    # the side problem is not conflated into a second status
    res = reconcile_fill(
        {"trade_id": "mts-auto-222204-082", "qty": 1, "price": 42765.0},
        42749.0, 10.0,
    )
    assert res.status == STATUS_CORRUPT
    assert res.issue_flags == ["corrupt_realized_pnl"]
