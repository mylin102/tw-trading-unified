"""T23-T29: real-data guards (codex deliverable-2 review blockers).

Each regression test pins one blocker:
  T23 per-leg qty (unequal legs)     T24 per-leg multiplier (contract code)
  T25 no silent LONG fallback        T26 no 0.0 entry-avg false PnL
  T27 fee wiring                     T28 latency side-aware bid/ask
  T29 deterministic status aggregation
"""
from datetime import datetime, timedelta
import pytest

from scripts.research.exit_attribution.pipeline import build_trade_row
from scripts.research.exit_attribution.reconcile import (
    STATUS_CORRUPT,
    STATUS_FEE_UNCERTAIN,
    STATUS_MISMATCH,
    STATUS_UNRECONCILED,
    aggregate_status,
    reconcile_fill,
)
from scripts.research.exit_attribution.stats import fixed_event_latency_quote

T0 = datetime(2026, 8, 6, 12, 0, 0)


def _ticks(rel_px=201.0, other_px=99.0, bid_ask=True):
    t = T0 + timedelta(minutes=10)
    return {
        "TMFN": [{"ts": t, "bid": 98.0 if bid_ask else 0,
                  "ask": 100.0 if bid_ask else 0, "price": 99.0}],
        # TMF bid 201 = executable SELL price (matches release fill px)
        "TMF": [{"ts": t, "bid": 201.0 if bid_ask else 0,
                 "ask": 202.0 if bid_ask else 0, "price": 201.0}],
    }


def test_t23_unequal_legs_qty():
    # NEAR 2 lots, FAR 1 lot; FAR released 1; NEAR mark/valuation must use 2
    trade = {
        "trade_id": "t23", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 2, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10), "realized_pnl": 8.0, "fees": 2.0},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 2, "price": 99.0,
             "ts": T0 + timedelta(minutes=20), "realized_pnl": 16.0, "fees": 4.0},
        ],
    }
    row = build_trade_row(trade, _ticks(), {"TMFN": 10.0, "TMF": 10.0, "DEFAULT": 10.0})
    assert row["status"] == "OK"
    # NEAR mark (BUY -> ask 100.0): (100-100)*2*10*(-1) = 0; FAR actual +10
    assert row["pre_release_paired_pnl"] == pytest.approx(10.0)
    # combined valuation: FAR (201-200)*1*10=10; NEAR (100-100)*2*10*-1=0
    assert row["release_time_combined_valuation_gross"] == pytest.approx(10.0)
    # actual gross: rel (10) + NEAR (20) = 30
    assert row["actual_full_pnl_gross"] == pytest.approx(30.0)


def test_t24_per_leg_multiplier_by_contract_code():
    # NEAR multiplier 5, FAR 10 -> each leg uses its own contract value
    trade = {
        "trade_id": "t24", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10), "realized_pnl": 8.0, "fees": 2.0},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + timedelta(minutes=20), "realized_pnl": 3.0, "fees": 2.0},
        ],
    }
    # FAR release +1pt * 10 = +10; NEAR mark (ask 100) = 0 -> pre = 10
    row = build_trade_row(trade, _ticks(), {"TMFN": 5.0, "TMF": 10.0, "DEFAULT": 10.0})
    assert row["pre_release_paired_pnl"] == pytest.approx(10.0)
    # NEAR exit (99-100)*1*5*-1 = +5 -> actual gross = 10 + 5 = 15
    assert row["actual_full_pnl_gross"] == pytest.approx(15.0)


def test_t25_missing_entry_side_no_long_default():
    trade = {
        "trade_id": "t25", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "", "qty": 1, "price": 200.0, "ts": T0},  # invalid
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10), "realized_pnl": 8.0, "fees": 2.0},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + timedelta(minutes=20), "realized_pnl": 8.0, "fees": 2.0},
        ],
    }
    row = build_trade_row(trade, _ticks(), {"TMFN": 10.0, "TMF": 10.0, "DEFAULT": 10.0})
    assert row["status"] == STATUS_UNRECONCILED
    assert "missing_position_side" in row["issue_flags"]
    assert row["pre_release_paired_pnl"] is None  # never fabricated


def test_t26_missing_entry_avg_unreconciled():
    trade = {
        "trade_id": "t26", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            # no FAR entry fills at all
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10), "realized_pnl": 8.0, "fees": 2.0},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + timedelta(minutes=20), "realized_pnl": 8.0, "fees": 2.0},
        ],
    }
    row = build_trade_row(trade, _ticks(), {"TMFN": 10.0, "TMF": 10.0, "DEFAULT": 10.0})
    assert row["status"] == STATUS_UNRECONCILED
    assert "missing_entry_avg" in row["issue_flags"]
    assert row["actual_full_pnl_gross"] is None  # no 0.0 false PnL


def test_t27_fee_wiring():
    # no fill fees + no schedule -> FEE_UNCERTAIN
    trade_no_fee = {
        "trade_id": "t27a", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10), "realized_pnl": 8.0},  # no fees key
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + timedelta(minutes=20), "realized_pnl": 8.0},  # no fees key
        ],
    }
    row = build_trade_row(trade_no_fee, _ticks(), {"TMFN": 10.0, "TMF": 10.0, "DEFAULT": 10.0})
    assert row["status"] == STATUS_FEE_UNCERTAIN
    assert "fee_missing" in row["issue_flags"]
    assert row["actual_full_pnl_net"] is None  # net excluded, gross reported
    assert row["actual_full_pnl_gross"] is not None

    # with schedule -> fee applied, status OK
    row2 = build_trade_row(
        trade_no_fee, _ticks(), {"TMFN": 10.0, "TMF": 10.0, "DEFAULT": 10.0},
        fee_schedule={"per_contract": 2.0},
    )
    assert row2["status"] == "OK"
    # net = rel net 8 (10-2) + NEAR net 8 (10-2) = 16
    assert row2["actual_full_pnl_net"] == pytest.approx(16.0)
    assert row2["actual_full_pnl_gross"] == pytest.approx(20.0)


def test_t28_latency_side_aware():
    T0 = datetime(2026, 8, 6, 12, 0, 0)
    ticks = [
        {"ts": T0 + timedelta(seconds=3), "ask": 102.0, "bid": 101.0, "price": 101.5},
    ]
    px, _ts, src = fixed_event_latency_quote(ticks, T0, delay_s=1.0, window_s=5.0, side="BUY")
    assert px == pytest.approx(102.0) and src == "ask"
    px2, _ts2, src2 = fixed_event_latency_quote(ticks, T0, delay_s=1.0, window_s=5.0, side="SELL")
    assert px2 == pytest.approx(101.0) and src2 == "bid"


def test_t29_deterministic_status_aggregation():
    # CORRUPT dominates MISMATCH; flags from ALL results preserved
    corrupt = reconcile_fill(
        {"trade_id": "mts-auto-222204-082", "side": "SELL", "qty": 1, "price": 42765.0},
        42749.0, 10.0,
    )
    mismatch = reconcile_fill(
        {"trade_id": "t29", "side": "SELL", "qty": 5, "price": 110.0},
        100.0, 10.0, expected_closed_qty=3,
    )
    status, flags = aggregate_status([mismatch, corrupt])
    assert status == STATUS_CORRUPT  # severity order, not list order
    assert "corrupt_realized_pnl" in flags and "leg_qty_mismatch" in flags
    # UNRECONCILED dominates MISMATCH
    unrec = reconcile_fill({"trade_id": "t29b", "qty": 1, "price": 110.0}, 100.0, 10.0)
    status2, flags2 = aggregate_status([mismatch, unrec])
    assert status2 == STATUS_UNRECONCILED
