"""F3 regression locks for audited recovery-abandon authority paths."""

import json
from datetime import datetime, timezone

from strategies.plugins.futures.active import tmf_spread
from strategies.futures.monitor import FuturesMonitor
from strategies.futures.mts_ledger_authority import MtsLedgerProjection


TRADE_ID = "mts-auto-022000-772"


def _entries(trade_id=TRADE_ID):
    return [
        {"trade_id": trade_id, "fill_type": "ENTRY", "leg": "NEAR",
         "qty": 1, "price": 46346.0, "side": "LONG",
         "timestamp": "2026-09-03T02:20:01+08:00"},
        {"trade_id": trade_id, "fill_type": "ENTRY", "leg": "FAR",
         "qty": 1, "price": 46543.0, "side": "SHORT",
         "timestamp": "2026-09-03T02:20:02+08:00"},
    ]


def _abandon(path):
    path.write_text(json.dumps({
        "event_type": "MTS_RECOVERY_ABANDON",
        "abandoned_trade_id": TRADE_ID,
        "command_id": "CMD-ABANDON-022000-772",
        "reason": "paper_only_no_broker_leg",
        "ts": "2026-09-04T00:51:17+00:00",
    }) + "\n")


def test_abandoned_json_state_is_not_restored(tmp_path, monkeypatch):
    fills = tmp_path / "fills.jsonl"
    audit = tmp_path / "recovery_audit.jsonl"
    state = tmp_path / "mts_position_state.json"
    fills.write_text("\n".join(json.dumps(x) for x in _entries()) + "\n")
    _abandon(audit)
    state.write_text(json.dumps({
        "has_position": True,
        "state": "OPEN",
        "trade_id": TRADE_ID,
        "_updated": datetime.now(timezone.utc).isoformat(),
        "near_entry": 46346.0,
        "far_entry": 46543.0,
        "near_side": "LONG",
        "far_side": "SHORT",
        "released_leg": None,
        "trail_peak": 0.0,
        "trail_nadir": 0.0,
        "entry_spread_z": 3.0,
        "state_revision": 1,
    }))

    monkeypatch.setattr(tmf_spread, "_MTS_FILL_LOG", str(fills))
    monkeypatch.setattr(tmf_spread, "_MTS_RECOVERY_AUDIT_LOG", str(audit))
    monkeypatch.setattr(tmf_spread, "_get_state_file_path", lambda: state)

    strategy = tmf_spread.TMFSpread.__new__(tmf_spread.TMFSpread)
    strategy._broker_truth_flat = False
    strategy._mts_recovery_state = None
    strategy._mts_state_write_enabled = False

    assert strategy._restore_position_state() is False
    assert strategy._mts_recovery_state is tmf_spread.RecoveryState.FLAT_CONFIRMED
    assert getattr(strategy, "_has_position", False) is not True


def test_abandoned_fills_are_not_open_at_monitor_consumer(tmp_path, monkeypatch):
    fills = tmp_path / "fills.jsonl"
    audit = tmp_path / "recovery_audit.jsonl"
    fills.write_text("\n".join(json.dumps(x) for x in _entries()) + "\n")
    _abandon(audit)
    monkeypatch.setattr(tmf_spread, "_MTS_RECOVERY_AUDIT_LOG", str(audit))

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._ledger_projection = MtsLedgerProjection(path=str(fills))

    assert monitor._mts_has_open_position_from_fills() is False


def test_non_abandoned_open_fills_remain_open(tmp_path, monkeypatch):
    fills = tmp_path / "fills.jsonl"
    audit = tmp_path / "recovery_audit.jsonl"
    fills.write_text("\n".join(json.dumps(x) for x in _entries("mts-normal-open")) + "\n")
    audit.write_text("")
    monkeypatch.setattr(tmf_spread, "_MTS_RECOVERY_AUDIT_LOG", str(audit))

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._ledger_projection = MtsLedgerProjection(path=str(fills))

    assert monitor._mts_has_open_position_from_fills() is True
