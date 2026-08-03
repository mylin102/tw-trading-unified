# P0 golden tests: side-sign correctness locked.
# 4 combinations + 3 hand-verified trades (2026-08-03).
import json
import os
from unittest.mock import patch

import pytest

from scripts.generate_daily_report import _compute_trade_pnl, _fill_pnl_components

GIT = os.path.expanduser("~/Documents/mylin102/tw-trading-unified-git")
FILLS = f"{GIT}/logs/mts_trade_fills.jsonl"


def _load_trade(tid):
    rows = [json.loads(l) for l in open(FILLS) if l.strip() and json.loads(l).get("trade_id") == tid]
    return {
        "entries": [r for r in rows if r.get("fill_type") == "ENTRY"],
        "exit_fills": [r for r in rows if r.get("fill_type") in ("EXIT", "COMBINED_EXIT") and r.get("qty", 0) > 0],
        "events": [], "trade_id": tid,
    }


# ── 4 side combinations ──────────────────────────────────────────────────
def _mk(leg_side, exit_side, entry_px, exit_px, qty=1, pv=10.0):
    fill = {"price": exit_px, "qty": qty, "leg": "NEAR", "fill_type": "COMBINED_EXIT",
            "position_side_before_exit": None}
    entry = {"price": entry_px, "side": leg_side, "leg": "NEAR"}
    return _fill_pnl_components(fill, entry, pv)


def test_near_long_exit_above_entry_positive():
    pnl, _, _, _, m = _mk("LONG", None, 100, 110)
    assert pnl == pytest.approx(100.0)  # (110-100)*1*10
    assert m == "entry-fill-side"


def test_near_short_exit_above_entry_negative():
    pnl, _, _, _, _ = _mk("SHORT", None, 110, 100)
    assert pnl == pytest.approx(100.0)  # (110-100)*1*10


def test_far_long_uses_exit_minus_entry():
    pnl, _, _, _, _ = _mk("LONG", None, 43100, 43349)
    assert pnl == pytest.approx(2490.0)


def test_far_short_uses_entry_minus_exit():
    pnl, _, _, _, _ = _mk("SHORT", None, 43618, 43622)
    assert pnl == pytest.approx(-40.0)


def test_side_resolution_canonical_position_wins():
    fill = {"price": 110.0, "qty": 1, "leg": "NEAR", "fill_type": "COMBINED_EXIT",
            "position_side_before_exit": "SHORT"}
    entry = {"price": 100.0, "side": "BUY", "leg": "NEAR"}  # contradicting legacy
    pnl, _, _, _, m = _fill_pnl_components(fill, entry, 10.0)
    assert pnl == pytest.approx(-100.0)  # SHORT wins over BUY entry
    assert m == "canonical-position_side_before_exit"


def test_unresolved_side_returns_none():
    fill = {"price": 110.0, "qty": 1, "leg": "NEAR", "fill_type": "COMBINED_EXIT",
            "position_side_before_exit": None}
    entry = {"price": 100.0, "side": "?", "leg": "NEAR"}
    pnl, _, _, _, m = _fill_pnl_components(fill, entry, 10.0)
    assert pnl is None
    assert m == "UNRESOLVED"


# ── Golden trades (hand-verified 2026-08-03) ─────────────────────────────
@pytest.mark.parametrize("tid,expected", [
    ("mts-auto-095647-268", 750.0),
    ("mts-auto-084500-110", 2500.0),
    ("mts-auto-101527-200", 60.0),
])
def test_golden_trade_pnl(tid, expected):
    if not os.path.exists(FILLS):
        pytest.skip("fills log not present")
    data = _load_trade(tid)
    pnl = _compute_trade_pnl(data, FILLS)
    assert pnl.get("gross_pnl") == pytest.approx(expected, abs=0.2), \
        f"{tid}: expected {expected}, got {pnl.get('gross_pnl')}"
    assert pnl.get("pnl_source") == "FILLS_BACKFILL"
    assert pnl.get("calculation_version") == "pnl-backfill-v2"


def test_leg_sum_matches_settlement_gross():
    """Σ per-leg backfill equals trade gross (invariant)."""
    if not os.path.exists(FILLS):
        pytest.skip("fills log not present")
    data = _load_trade("mts-auto-084500-110")
    total = 0.0
    for f in data["exit_fills"]:
        e = next((x for x in data["entries"] if x.get("leg") == f.get("leg")), None)
        pnl, _, _, _, _ = _fill_pnl_components(f, e, 10.0)
        total += pnl
    assert total == pytest.approx(2500.0, abs=0.2)
