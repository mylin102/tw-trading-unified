"""T36-T39: overclose + mixed/unknown release-leg guards (codex v3 review).

  T36 overclosed release (closed > open) -> UNRECONCILED + overclosed_release
  T37 overclosed sibling exit -> UNRECONCILED + overclosed_sibling_exit
  T38 mixed release legs -> UNRECONCILED + mixed_release_legs (never ignored)
  T39 unknown release leg -> UNRECONCILED + unknown_release_leg
All must yield attribution UNKNOWN (missing PnL never treated as 0/NEUTRAL).
"""
from datetime import datetime, timedelta
import pytest

from scripts.research.exit_attribution.pipeline import build_trade_row

T0 = datetime(2026, 8, 6, 12, 0, 0)
MULT = {"TMFN": 10.0, "TMF": 10.0, "DEFAULT": 10.0}
FEE0 = {"per_contract": 0.0}


def _ticks():
    t = T0 + timedelta(minutes=10)
    return {
        "TMFN": [{"ts": t, "bid": 98.0, "ask": 100.0, "price": 99.0}],
        "TMF": [{"ts": t, "bid": 201.0, "ask": 202.0, "price": 201.0}],
    }


def test_t36_overclosed_release():
    trade = {
        "trade_id": "t36", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 202.0,  # overfill
             "ts": T0 + timedelta(minutes=11)},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + timedelta(minutes=20)},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "UNRECONCILED"
    assert "overclosed_release" in row["issue_flags"]
    assert row["actual_full_pnl_gross"] is None
    assert row["entry_attribution"] == "UNKNOWN"  # never silent NEUTRAL/NOT_BAD
    assert row["release_attribution"] == "UNKNOWN"


def test_t37_overclosed_sibling_exit():
    trade = {
        "trade_id": "t37", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 2, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 3, "price": 99.0,  # 3 > 2 open
             "ts": T0 + timedelta(minutes=20)},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "UNRECONCILED"
    assert "overclosed_sibling_exit" in row["issue_flags"]
    assert row["actual_full_pnl_gross"] is None
    assert row["release_attribution"] == "UNKNOWN"


def test_t38_mixed_release_legs():
    trade = {
        "trade_id": "t38", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,  # other leg
             "ts": T0 + timedelta(minutes=11)},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + timedelta(minutes=20)},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "UNRECONCILED"
    assert "mixed_release_legs" in row["issue_flags"]
    assert row["actual_full_pnl_gross"] is None


def test_t39_unknown_release_leg():
    trade = {
        "trade_id": "t39", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "XYZ", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10)},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + timedelta(minutes=20)},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "UNRECONCILED"
    assert "unknown_release_leg" in row["issue_flags"]
    assert row["actual_full_pnl_gross"] is None
