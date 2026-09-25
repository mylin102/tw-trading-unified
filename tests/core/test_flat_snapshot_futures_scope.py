"""A stock account's holdings must never hide a reconciled futures spread.

Live 2026-09-25 01:42: the app's own post-startup gate archived a capture=OK
snapshot holding the real spread (futures TMFJ6 1 / TMFK6 1) *plus* the
account's STOCK rows, and its ctx carried the operator-recorded
canonical_holding for those same two legs -- and the gate still refused with
GUARD_POSITION_NOT_FLAT.  The guard handed the UNFILTERED position list to
is_reconciled_mts_position(), which requires len(positions) in (1, 2), so any
stock holding made the spread unrecognizable.  The deploy gate passes because
the read-only capture contains futures rows only.
"""
import json
import time
from pathlib import Path

from core.deployment_safety_gate import guard_flat_no_pending

STOCK_ROWS = [
    {"account": "stock", "code": "00919", "quantity": 2,
     "direction": "Action.Buy", "avg_cost": 28.68, "pnl": 7680.0},
    {"account": "stock", "code": "2059", "quantity": 0,
     "direction": "Action.Buy", "avg_cost": 12539.2, "pnl": -1216.0},
]
FUTURES_ROWS = [
    {"account": "futures", "code": "TMFJ6", "quantity": 1,
     "direction": "Action.Sell", "avg_cost": 48176.0, "pnl": 2830.0},
    {"account": "futures", "code": "TMFK6", "quantity": 1,
     "direction": "Action.Buy", "avg_cost": 48320.0, "pnl": -2730.0},
]
HOLDING = {
    "canonical_legs": [
        {"code": "TMFJ6", "side": "SHORT", "qty": 1, "avg_cost": 48176.0},
        {"code": "TMFK6", "side": "LONG", "qty": 1, "avg_cost": 48320.0}],
    "source": "broker_reconciled_record",
    "captured_at": int(time.time() * 1000),
    "canonical_input_hash": "c" * 64,
}


def _snapshot(path: Path, positions, **over):
    data = {"source": "live_broker", "mode": "live", "positions": positions,
            "open_orders": [], "captured_at": int(time.time() * 1000),
            "canonical_input_hash": "b" * 64, "session_id": "sess-1",
            "account_identity_hash": "a" * 64}
    data.update(over)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _ctx(canonical_holding=HOLDING):
    return {"requested_mode": "live", "effective_mode": "live_quarantined",
            "live_order_allowed": False, "audit_reasons": [], "revision": 4,
            "updated_at": "2026-09-25T01:42:58Z", "account_id_hash": None,
            "session_id": "sess-1", "process_start_id": "p1",
            "config_hash": None, "state_namespace": "live",
            "canonical_holding": canonical_holding}


def test_stock_rows_do_not_hide_a_reconciled_spread(tmp_path):
    """The exact live shape: stock holdings + the futures spread + the recorded
    broker truth -> the guard must recognise the existing MTS position."""
    pf = _snapshot(tmp_path / "logs/position_state.json", STOCK_ROWS + FUTURES_ROWS)
    r = guard_flat_no_pending(str(pf), _ctx(), allow_existing_mts_position=True)
    assert r.ok, (r.reasons, r.detail)
    assert "reconciled" in r.detail


def test_stock_rows_never_block_mts_flat(tmp_path):
    """Stock holdings are not futures risk: they must not fail the flat path."""
    pf = _snapshot(tmp_path / "logs/position_state.json", STOCK_ROWS)
    r = guard_flat_no_pending(str(pf), _ctx(canonical_holding=None))
    assert r.ok, (r.reasons, r.detail)


def test_without_the_recorded_holding_the_same_snapshot_is_refused(tmp_path):
    """Fail closed: the futures legs alone are not an authorized adoption."""
    pf = _snapshot(tmp_path / "logs/position_state.json", STOCK_ROWS + FUTURES_ROWS)
    r = guard_flat_no_pending(str(pf), _ctx(canonical_holding=None),
                              allow_existing_mts_position=True)
    assert not r.ok and "GUARD_POSITION_NOT_FLAT" in r.reasons


def test_without_the_operator_flag_the_snapshot_is_refused(tmp_path):
    pf = _snapshot(tmp_path / "logs/position_state.json", STOCK_ROWS + FUTURES_ROWS)
    r = guard_flat_no_pending(str(pf), _ctx(), allow_existing_mts_position=False)
    assert not r.ok and "GUARD_POSITION_NOT_FLAT" in r.reasons


def test_legacy_untagged_rows_still_work(tmp_path):
    """Rows without an `account` key stay futures-relevant (no regression)."""
    legacy = [dict(FUTURES_ROWS[0]), dict(FUTURES_ROWS[1])]
    legacy[0].pop("account")
    legacy[1].pop("account")
    pf = _snapshot(tmp_path / "logs/position_state.json", STOCK_ROWS + legacy)
    r = guard_flat_no_pending(str(pf), _ctx(), allow_existing_mts_position=True)
    assert r.ok, (r.reasons, r.detail)
