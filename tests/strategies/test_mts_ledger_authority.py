#!/usr/bin/env python3
"""TDD red phase: MTS ledger authority tests (three-state + dual gate)."""

import json
import os
import shutil
import tempfile

import pytest

from strategies.futures.mts_ledger_authority import (
    MtsAuthority,
    MtsAuthorityState,
    MtsGateAction,
    MtsLedgerProjection,
    gate_decision_post_signal,
    gate_decision_pre_signal,
    project_fills,
)


def _fill(tid, leg, side, price, ft, qty=1, ts="2026-08-06T09:14:03.000001"):
    return {"timestamp": ts, "ticker": "TMF", "contract": leg, "leg": leg,
            "side": side, "qty": qty, "price": price, "fill_type": ft,
            "trade_id": tid}


# ── pure projection ──

def test_incident_reproduction_three_state():
    fills = [
        _fill("mts-auto-091403-145", "NEAR", "SHORT", 44251.0, "ENTRY"),
        _fill("mts-auto-091403-145", "FAR", "LONG", 44177.0, "ENTRY"),
    ]
    auth = project_fills(fills)
    assert auth.status == MtsAuthority.OPEN
    assert auth.trade_id == "mts-auto-091403-145"
    assert auth.near_qty == -1 and auth.far_qty == +1
    assert auth.near_side == "SHORT" and auth.far_side == "LONG"


def test_closed_trade_is_flat():
    fills = [
        _fill("t1", "NEAR", "SHORT", 44251.0, "ENTRY"),
        _fill("t1", "FAR", "LONG", 44177.0, "ENTRY"),
        _fill("t1", "NEAR", "BUY", 44306.0, "EXIT", ts="2026-08-06T09:20:00"),
        _fill("t1", "FAR", "SELL", 44359.0, "EXIT", ts="2026-08-06T09:20:01"),
    ]
    assert project_fills(fills).status == MtsAuthority.FLAT


def test_partial_and_release_qty_remaining():
    fills = [
        _fill("t2", "NEAR", "SHORT", 44100.0, "ENTRY", qty=2),
        _fill("t2", "FAR", "LONG", 44200.0, "ENTRY", qty=2),
        _fill("t2", "FAR", "SELL", 44150.0, "RELEASE", qty=1, ts="2026-08-06T10:00:00"),
    ]
    auth = project_fills(fills)
    assert auth.status == MtsAuthority.OPEN
    assert auth.near_qty == -2 and auth.far_qty == +1


def test_qty_two_and_duplicate_deal_dedup():
    dup = _fill("t3", "NEAR", "SHORT", 44100.0, "ENTRY", qty=2)
    auth = project_fills([dup, dup])
    assert auth.near_qty == -2, "duplicate deal must not double count"


def test_old_trade_leftover_does_not_protect_current():
    # old trade A has an unclosed entry; current trade B is also open
    fills = [
        _fill("mts-auto-A", "NEAR", "SHORT", 44000.0, "ENTRY", ts="2026-08-06T08:00:00"),
        _fill("mts-auto-B", "NEAR", "LONG", 44100.0, "ENTRY", ts="2026-08-06T09:00:00"),
        _fill("mts-auto-B", "FAR", "SHORT", 44200.0, "ENTRY", ts="2026-08-06T09:00:01"),
    ]
    auth = project_fills(fills)
    assert auth.status == MtsAuthority.OPEN
    assert auth.trade_id == "mts-auto-B", "authority must anchor on the CURRENT trade"
    assert auth.near_qty == +1 and auth.far_qty == -1


def test_old_leftover_alone_is_flat_not_open():
    # old trade A unclosed, but the latest trade B fully closed → FLAT
    fills = [
        _fill("mts-auto-A", "NEAR", "SHORT", 44000.0, "ENTRY", ts="2026-08-06T08:00:00"),
        _fill("mts-auto-B", "NEAR", "LONG", 44100.0, "ENTRY", ts="2026-08-06T09:00:00"),
        _fill("mts-auto-B", "NEAR", "SELL", 44150.0, "EXIT", ts="2026-08-06T09:30:00"),
    ]
    auth = project_fills(fills)
    assert auth.status == MtsAuthority.FLAT
    assert auth.trade_id is None


def test_invalid_side_fails_closed():
    fills = [_fill("t4", "NEAR", "NEAR", 44251.0, "ENTRY")]  # garbage side
    auth = project_fills(fills)
    assert auth.near_qty == 0
    assert auth.near_side is None, "invalid side must not fabricate LONG/SHORT"


def test_entry_prices_captured():
    fills = [
        _fill("t5", "NEAR", "SHORT", 44251.0, "ENTRY"),
        _fill("t5", "FAR", "LONG", 44177.0, "ENTRY"),
    ]
    auth = project_fills(fills)
    assert auth.near_entry == 44251.0 and auth.far_entry == 44177.0


# ── pre-signal gate (three-state, no one-line guard) ──

def test_incident_gate_reconstruct_not_reset():
    """state flat + memory open + same-trade ledger open → RECONSTRUCT, not RESET."""
    auth = MtsAuthorityState(MtsAuthority.OPEN, trade_id="mts-auto-091403-145",
                             near_qty=-1, far_qty=1, near_side="SHORT", far_side="LONG")
    action = gate_decision_pre_signal(auth, state_has_pos=False,
                                      strat_has_pos=True, strat_trade_id="mts-auto-091403-145")
    assert action == MtsGateAction.RECONSTRUCT


def test_true_divergence_resets():
    auth = MtsAuthorityState(MtsAuthority.FLAT)
    action = gate_decision_pre_signal(auth, state_has_pos=False,
                                      strat_has_pos=True, strat_trade_id="t-x")
    assert action == MtsGateAction.RESET_STRATEGY


def test_ledger_unreadable_never_resets():
    auth = MtsAuthorityState(MtsAuthority.UNKNOWN)
    action = gate_decision_pre_signal(auth, state_has_pos=False,
                                      strat_has_pos=True, strat_trade_id="t-x")
    assert action == MtsGateAction.PASS, "UNKNOWN must never reset"


def test_strategy_lost_position_reconstructs():
    auth = MtsAuthorityState(MtsAuthority.OPEN, trade_id="t-live",
                             near_qty=-1, far_qty=1, near_side="SHORT", far_side="LONG")
    action = gate_decision_pre_signal(auth, state_has_pos=False,
                                      strat_has_pos=False, strat_trade_id=None)
    assert action == MtsGateAction.RECONSTRUCT


def test_stale_strategy_trade_reconstructs():
    auth = MtsAuthorityState(MtsAuthority.OPEN, trade_id="t-new",
                             near_qty=-1, far_qty=1, near_side="SHORT", far_side="LONG")
    action = gate_decision_pre_signal(auth, state_has_pos=True,
                                      strat_has_pos=True, strat_trade_id="t-old")
    assert action == MtsGateAction.RECONSTRUCT


# ── post-signal gate ──

def test_exit_not_blocked_when_ledger_open():
    auth = MtsAuthorityState(MtsAuthority.OPEN, trade_id="t1", near_qty=-1, far_qty=1)
    assert gate_decision_post_signal(auth, "EXIT") == MtsGateAction.PASS
    assert gate_decision_post_signal(auth, "PARTIAL_EXIT") == MtsGateAction.PASS


def test_exit_blocked_when_flat():
    auth = MtsAuthorityState(MtsAuthority.FLAT)
    assert gate_decision_post_signal(auth, "EXIT") == MtsGateAction.BLOCK_SIGNAL


def test_exit_allowed_when_unknown_fail_open():
    auth = MtsAuthorityState(MtsAuthority.UNKNOWN)
    assert gate_decision_post_signal(auth, "EXIT") == MtsGateAction.PASS


def test_non_exit_signal_never_blocked():
    auth = MtsAuthorityState(MtsAuthority.FLAT)
    assert gate_decision_post_signal(auth, "SELL_NEAR_BUY_FAR") == MtsGateAction.PASS


# ── incremental projection (no per-tick full scan) ──

def test_incremental_projection_syncs_new_rows_only(tmp_path):
    path = tmp_path / "fills.jsonl"
    proj = MtsLedgerProjection(path=str(path), source="PAPER")
    with open(path, "w") as f:
        f.write(json.dumps(_fill("t6", "NEAR", "SHORT", 44100.0, "ENTRY")) + "\n")
    assert proj.sync_from_ledger() == 1
    auth = proj.snapshot()
    assert auth.status == MtsAuthority.OPEN and auth.near_qty == -1

    # append the far entry + an EXIT — only NEW bytes are read
    with open(path, "a") as f:
        f.write(json.dumps(_fill("t6", "FAR", "LONG", 44200.0, "ENTRY")) + "\n")
        f.write(json.dumps(_fill("t6", "NEAR", "BUY", 44150.0, "EXIT", ts="2026-08-06T10:00:00")) + "\n")
    assert proj.sync_from_ledger() == 2
    auth = proj.snapshot()
    assert auth.status == MtsAuthority.OPEN and auth.near_qty == 0 and auth.far_qty == 1


def test_incremental_projection_unreadable():
    proj = MtsLedgerProjection(path="/nonexistent/fills.jsonl", source="LIVE")
    assert proj.sync_from_ledger() == 0
    assert proj.snapshot().status == MtsAuthority.UNKNOWN


def test_paper_live_source_separation():
    fills = [_fill("t7", "NEAR", "SHORT", 44100.0, "ENTRY")]
    p1 = MtsLedgerProjection(path="x", source="PAPER")
    p2 = MtsLedgerProjection(path="x", source="LIVE")
    for p in (p1, p2):
        for f in fills:
            p.apply_fill(f)
    assert p1.source == "PAPER" and p2.source == "LIVE"
    assert p1.snapshot() == p2.snapshot(), "identical fills → identical authority regardless of source"


def test_projection_rotation_handles_truncated_file(tmp_path):
    path = tmp_path / "fills.jsonl"
    proj = MtsLedgerProjection(path=str(path), source="PAPER")
    with open(path, "w") as f:
        f.write(json.dumps(_fill("t8", "NEAR", "SHORT", 44100.0, "ENTRY")) + "\n")
    proj.sync_from_ledger()
    # simulate rotation: file replaced with a shorter one
    with open(path, "w") as f:
        f.write(json.dumps(_fill("t9", "FAR", "LONG", 44200.0, "ENTRY")) + "\n")
    proj.sync_from_ledger()
    auth = proj.snapshot()
    assert auth.status == MtsAuthority.OPEN and auth.trade_id == "t9"
