"""T13/T14/T22: attribution axes independence + proxy-never-executable."""
import pytest

from scripts.research.exit_attribution.classify import (
    ENTRY_BAD,
    ENTRY_UNKNOWN,
    REL_HARMFUL,
    REL_UNKNOWN,
    classify_row,
)


def test_t13_axes_independent_both_bad():
    row = {
        "data_quality": "OK",
        "pre_release_paired_pnl": -100.0,   # entry already bad
        "post_release_incremental_pnl": -50.0,  # release also harmful
    }
    c = classify_row(row)
    assert c["entry_attribution"] == ENTRY_BAD
    assert c["release_attribution"] == REL_HARMFUL  # BOTH set, not forced one-of


def test_t14_unknown_when_data_quality_not_ok():
    for dq in ("PROXY", "UNUSABLE", "UNRECONCILED"):
        c = classify_row({"data_quality": dq})
        assert c["entry_attribution"] == ENTRY_UNKNOWN
        assert c["release_attribution"] == REL_UNKNOWN


def test_t22_proxy_never_executable():
    from scripts.research.exit_attribution.pipeline import build_trade_row
    from datetime import datetime

    T0 = datetime(2026, 8, 6, 12, 0, 0)
    trade = {
        "trade_id": "t-proxy",
        "entries": [
            {"leg": "NEAR", "side": "SHORT", "qty": 1, "price": 100.0, "ts": T0},
            {"leg": "FAR", "side": "LONG", "qty": 1, "price": 200.0, "ts": T0},
        ],
        "releases": [
            {"leg": "FAR", "side": "SELL", "qty": 1, "price": 201.0,
             "ts": T0 + __import__("datetime").timedelta(minutes=10),
             "realized_pnl": 10.0},
        ],
        "exits": [
            {"leg": "NEAR", "side": "BUY", "qty": 1, "price": 99.0,
             "ts": T0 + __import__("datetime").timedelta(minutes=20),
             "realized_pnl": 10.0},
        ],
    }
    # NEAR leg has no bid/ask -> BOUNDED_TICK_PROXY (proxy), FAR has BBO
    ticks = {
        "TMFN": [{"ts": T0 + __import__("datetime").timedelta(minutes=10),
                  "bid": 0, "ask": 0, "price": 99.5}],
        "TMF": [{"ts": T0 + __import__("datetime").timedelta(minutes=10),
                 "bid": 200.5, "ask": 201.5, "price": 201.0}],
    }
    row = build_trade_row(trade, ticks, {"NEAR": 10.0, "FAR": 10.0, "DEFAULT": 10.0})
    assert row["valuation_tier"] == "BOUNDED_TICK_PROXY"
    assert row["immediate_executable_combined_pnl_gross"] is None  # NEVER executable
    assert row["release_time_combined_valuation_gross"] is not None  # valuation OK
