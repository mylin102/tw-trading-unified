"""T20/T21: pipeline end-to-end + fee effective-date selection."""
from datetime import datetime, timedelta
import pytest

from scripts.research.exit_attribution.pipeline import SCHEMA_VERSION, build_rows
from scripts.research.exit_attribution.fee import select_fee_schedule

T0 = datetime(2026, 8, 6, 12, 0, 0)
MULT = {"NEAR": 10.0, "FAR": 10.0, "DEFAULT": 10.0}


def _trade(tid, entry_near, entry_far, rel_px, exit_px, rel_leg="FAR",
           rel_dt=None, exit_dt=None):
    rel_dt = rel_dt or (T0 + timedelta(minutes=10))
    exit_dt = exit_dt or (T0 + timedelta(minutes=20))
    # stored realized = recomputed net (FAR LONG +1; NEAR SHORT -1; fee 2.0)
    rel_net = (rel_px - entry_far) * 10 * 1 - 2.0
    exit_net = (exit_px - entry_near) * 10 * -1 - 2.0
    return {
        "trade_id": tid,
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": entry_near, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": entry_far, "ts": T0},
        ],
        "releases": [
            {"leg": rel_leg, "side": "SELL", "qty": 1, "price": rel_px,
             "ts": rel_dt, "realized_pnl": rel_net, "fees": 2.0},
        ],
        "exits": [
            {"leg": "NEAR" if rel_leg == "FAR" else "FAR", "side": "BUY",
             "qty": 1, "price": exit_px, "ts": exit_dt, "realized_pnl": exit_net,
             "fees": 2.0},
        ],
    }


def _bbo_ticks():
    t = T0 + timedelta(minutes=10)
    return {
        "TMFN": [{"ts": t, "bid": 99.0, "ask": 100.0, "price": 99.5}],
        "TMF": [{"ts": t, "bid": 200.0, "ask": 201.0, "price": 200.5}],
    }


def test_t20_pipeline_end_to_end_deterministic():
    trades = [
        _trade("t-1", 100.0, 200.0, 201.0, 99.0),    # FAR released +10, NEAR +10 -> helpful
        _trade("t-2", 100.0, 200.0, 199.0, 99.0),    # FAR released -10 -> harmful
        _trade("t-3", 100.0, 200.0, 199.0, 100.5),   # release loss -> ENTRY_BAD (pre-release)
    ]
    rows = build_rows(trades, _bbo_ticks(), MULT)
    assert len(rows) == 3
    for r in rows:
        assert r["schema_version"] == SCHEMA_VERSION
        for key in ("trade_id", "release_ts", "pre_release_paired_pnl",
                    "release_time_combined_valuation_gross", "valuation_tier",
                    "immediate_executable_combined_pnl_gross",
                    "actual_full_pnl_gross", "post_release_incremental_pnl",
                    "unhedged_seconds", "status", "issue_flags", "data_quality",
                    "entry_attribution", "release_attribution"):
            assert key in r
    # t-1: FAR released at 201 (LONG +10), NEAR marked 99.5 short entry 100 (+5)
    #      -> pre_release positive; actual NEAR exit 99 -> +10 more -> HELPFUL
    r1 = rows[0]
    assert r1["valuation_tier"] == "EXECUTABLE_BBO"
    assert r1["immediate_executable_combined_pnl_gross"] is not None
    assert r1["release_attribution"] == "HELPFUL"
    assert r1["entry_attribution"] == "NOT_BAD"
    # t-3: NEAR SHORT entry 100, exits 100.5 -> loss -> ENTRY_BAD
    assert rows[2]["entry_attribution"] == "BAD"
    # deterministic: second run identical
    rows2 = build_rows(trades, _bbo_ticks(), MULT)
    assert [r["post_release_incremental_pnl"] for r in rows2] == \
           [r["post_release_incremental_pnl"] for r in rows]


def test_t21_fee_effective_date_selection():
    schedules = [
        {"effective_date": "2026-01-01", "per_contract": 5.0},
        {"effective_date": "2026-06-01", "per_contract": 7.0},
    ]
    assert select_fee_schedule(schedules, "2026-08-01")["per_contract"] == 7.0
    assert select_fee_schedule(schedules, "2026-03-01")["per_contract"] == 5.0
    # before earliest effective date -> rejected (None), never invented
    assert select_fee_schedule(schedules, "2025-12-31") is None
    assert select_fee_schedule([], "2026-08-01") is None
