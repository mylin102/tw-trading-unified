"""T40-T41: codex D3 final data-correctness guards.

  T40 corrupt trade actual_full is None (never fabricated 0.0)
  T41 stored_realized_mismatch flag + reconcile_diagnostics survive higher
      severity (UNRECONCILED) aggregation
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


def test_t40_corrupt_trade_actual_full_none_not_zero():
    trade = {
        "trade_id": "mts-auto-222204-082", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"trade_id": "mts-auto-222204-082", "leg": "NEAR", "side": "SHORT", "qty": 1, "price": 42596.0, "ts": T0},
            {"trade_id": "mts-auto-222204-082", "leg": "FAR", "side": "LONG", "qty": 1, "price": 42749.0, "ts": T0},
        ],
        "releases": [
            {"trade_id": "mts-auto-222204-082", "leg": "FAR", "side": "SELL", "qty": 1, "price": 42765.0,
             "ts": T0 + timedelta(minutes=10), "realized_pnl": -427698.6},
        ],
        "exits": [
            {"trade_id": "mts-auto-222204-082", "leg": "NEAR", "side": "BUY", "qty": 1, "price": 42779.0,
             "ts": T0 + timedelta(minutes=20), "realized_pnl": -1887.1},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "CORRUPT"
    assert row["actual_full_pnl_gross"] is None  # NEVER 0.0 for corrupt
    assert row["actual_full_pnl_net"] is None
    assert row["post_release_incremental_pnl"] is None


def test_t41_stored_mismatch_survives_higher_severity():
    # release fill's stored realized is wrong-signed (mismatch), while the
    # sibling is overclosed (higher severity UNRECONCILED): the mismatch must
    # survive as an additive flag + diagnostics, not be masked away.
    trade = {
        "trade_id": "t41", "near_code": "TMFN", "far_code": "TMF",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 2, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + timedelta(minutes=10), "realized_pnl": -3667.3},  # wrong-signed
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 3, "price": 99.0,  # 3 > 2 open
             "ts": T0 + timedelta(minutes=20), "realized_pnl": 16.0},
        ],
    }
    row = build_trade_row(trade, _ticks(), MULT, fee_schedule=FEE0)
    assert row["status"] == "UNRECONCILED"  # overclosed dominates severity
    assert "overclosed_sibling_exit" in row["issue_flags"]
    assert "stored_realized_mismatch" in row["issue_flags"]  # NOT masked
    diag = row.get("reconcile_diagnostics") or []
    assert any(d.get("delta") is not None for d in diag)  # delta exposed
    assert row["actual_full_pnl_gross"] is None
