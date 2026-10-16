"""Regression lock for audited paper-only recovery abandon records."""

import json

from strategies.plugins.futures.active import tmf_spread


TRADE_ID = "mts-auto-022000-772"


def _entry(leg, price):
    return {
        "trade_id": TRADE_ID,
        "fill_type": "ENTRY",
        "leg": leg,
        "qty": 1,
        "price": price,
        "side": "BUY" if leg == "NEAR" else "SELL",
        "mode": "paper",
        "session": "night",
        "session_id": "20260902_172129",
        "timestamp": "2026-09-03T02:20:01+08:00",
    }


def test_audited_paper_abandon_skips_fills_recovery(tmp_path, monkeypatch):
    fills = tmp_path / "fills.jsonl"
    audit = tmp_path / "recovery_audit.jsonl"
    state = tmp_path / "mts_position_state.json"
    fills.write_text("\n".join(json.dumps(x) for x in (
        _entry("NEAR", 46346.0), _entry("FAR", 46543.0))) + "\n")
    audit.write_text(json.dumps({
        "event_type": "MTS_RECOVERY_ABANDON",
        "abandoned_trade_id": TRADE_ID,
        "command_id": "CMD-ABANDON-022000-772",
        "reason": "paper_only_no_broker_leg",
        "ts": "2026-09-03T07:34:00+08:00",
    }) + "\n")
    state.write_text(json.dumps({
        "has_position": False,
        "state": "FLAT",
        "trade_id": "",
    }))

    monkeypatch.setattr(tmf_spread, "_MTS_FILL_LOG", str(fills))
    monkeypatch.setattr(tmf_spread, "_MTS_RECOVERY_AUDIT_LOG", str(audit), raising=False)
    monkeypatch.setattr(tmf_spread, "_get_state_file_path", lambda: str(state))

    strategy = tmf_spread.TMFSpread.__new__(tmf_spread.TMFSpread)
    strategy._broker_truth_flat = False
    strategy._mts_recovery_state = None
    strategy._mts_state_write_enabled = False

    assert strategy._restore_position_state() is False
    assert strategy._mts_recovery_state is tmf_spread.RecoveryState.FLAT_CONFIRMED
    assert getattr(strategy, "_has_position", False) is not True
    assert json.loads(state.read_text())["state"] == "FLAT"
    assert json.loads(state.read_text())["trade_id"] == ""
