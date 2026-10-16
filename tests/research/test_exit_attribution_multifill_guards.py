"""T30-T35: multi-fill / full-close / entry-coherence guards (codex P0 #1/#2).

  T30 two release fills same leg aggregated
  T31 two sibling exit fills aggregated
  T32 release only, no sibling exit -> UNRECONCILED, actual_full None
  T33 partial close with residual qty -> UNRECONCILED, actual_full None
  T34 conflicting entry sides rejected
  T35 zero/missing entry price rejected
"""
from datetime import datetime, timedelta
import pytest

from scripts.research.exit_attribution.pipeline import build_trade_row

T0 = datetime(2026, 8, 6, 12, 0, 0)
MULT = {"TMFN": 10.0, "TMF": 10.0, "DEFAULT": 10.0}
FEE0 = {"per_contract": 0.0}  # fees present at 0 -> status OK, no FEE_UNCERTAIN


def _ticks():
    t = T0 + timedelta(minutes=10)
    return {
        "TMFN": [{"ts": t, "bid": 98.0, "ask": 100.0, "price": 99.0}],
        "TMF": [{"ts": t, "bid": 201.0, "ask": 202.0, "price": 201.0}],
    }


def test_t30_two_release_fills_same_leg():
    trade = {
        "trade_id": "t30", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 0.5, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
            {"leg": "FAR", "side": "SELL", "qty": 0.5, "price": 202.0,
             "ts": T0 + timedelta(minutes=11)},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + timedelta(minutes=20)},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "OK"
    assert row["release_fill_count"] == 2
    # FAR wavg 201.5 -> (201.5-200)*1*10 = +15; NEAR (99-100)*1*10*-1 = +10
    assert row["actual_full_pnl_gross"] == pytest.approx(25.0)
    # pre: FAR actual 15 + NEAR mark (ask 100) 0 = 15
    assert row["pre_release_paired_pnl"] == pytest.approx(15.0)
    assert row["post_release_incremental_pnl"] == pytest.approx(10.0)


def test_t31_two_sibling_exit_fills():
    trade = {
        "trade_id": "t31", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 2, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + timedelta(minutes=20)},
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 100.0,
             "ts": T0 + timedelta(minutes=21)},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "OK"
    assert row["sibling_exit_fill_count"] == 2
    # NEAR: (99-100)*10*-1 + (100-100)*10*-1 = 10 + 0 = 10; FAR 10 -> 20
    assert row["actual_full_pnl_gross"] == pytest.approx(20.0)


def test_t32_release_only_no_sibling_exit():
    trade = {
        "trade_id": "t32", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
        ],
        "exits": [],  # sibling NEVER closed -> NOT a settled trade
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "UNRECONCILED"
    assert "missing_sibling_exit" in row["issue_flags"]
    assert row["actual_full_pnl_gross"] is None  # never fabricated settled
    assert row["post_release_incremental_pnl"] is None


def test_t33_partial_close_residual_qty():
    trade = {
        "trade_id": "t33", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 2, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,  # 1 of 2 closed
             "ts": T0 + timedelta(minutes=20)},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "UNRECONCILED"
    assert "partial_sibling_exit" in row["issue_flags"]
    assert row["actual_full_pnl_gross"] is None


def test_t34_conflicting_entry_sides():
    trade = {
        "trade_id": "t34", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "NEAR", "side": "LONG", "qty": 1, "price": 101.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 2, "price": 99.0,
             "ts": T0 + timedelta(minutes=20)},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "UNRECONCILED"
    assert "conflicting_entry_sides" in row["issue_flags"]
    assert row["actual_full_pnl_gross"] is None


def test_t35_zero_or_missing_entry_price():
    trade = {
        "trade_id": "t35", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 0, "ts": T0},  # zero px
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 2, "price": 99.0,
             "ts": T0 + timedelta(minutes=20)},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "UNRECONCILED"
    assert "zero_or_missing_entry_price" in row["issue_flags"]
    assert row["actual_full_pnl_gross"] is None
