import json
from pathlib import Path

from core.reconciliation.holding_state import ReconciliationStatus, reconcile_holding


def _leg(code, side="LONG", qty=1, avg_cost=100.0):
    return {"code": code, "side": side, "qty": qty, "avg_cost": avg_cost}


def _capture(positions, digest="a" * 64):
    return {"source": "live_broker", "positions": positions,
            "canonical_input_hash": digest, "captured_at": 1726579200000,
            "source_path": "/tmp/broker_snapshot.json"}


def test_t17_broker_flat_converges_and_does_not_hold(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"positions": [_leg("TMFI6"), _leg("TMFJ6", "SHORT")] }))
    decision = reconcile_holding(
        broker_legs=[], local_legs=[_leg("TMFI6"), _leg("TMFJ6", "SHORT")],
        tick_size=1.0, state_path=state, audit_path=tmp_path / "audit.json",
        alert_path=tmp_path / "alerts.jsonl", broker_capture=_capture([]))
    assert decision.status is ReconciliationStatus.RECONCILED
    assert decision.canonical_legs == ()
    assert decision.fail_closed is False
    assert json.loads(state.read_text())["positions"] == []


def test_t18_broker_holding_converges_from_local_flat(tmp_path):
    broker = [_leg("TMFI6"), _leg("TMFJ6", "SHORT")]
    decision = reconcile_holding(
        broker_legs=broker, local_legs=[], tick_size=1.0,
        state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.json",
        alert_path=tmp_path / "alerts.jsonl", broker_capture=_capture(broker))
    assert decision.status is ReconciliationStatus.RECONCILED
    assert decision.canonical_legs == tuple(broker)


def test_t19_unavailable_still_holds_and_does_not_converge(tmp_path):
    state = tmp_path / "state.json"
    old = [_leg("TMFI6")]
    state.write_text(json.dumps({"positions": old}))
    decision = reconcile_holding(
        broker_legs=None, local_legs=old, tick_size=1.0,
        state_path=state, audit_path=tmp_path / "audit.json",
        alert_path=tmp_path / "alerts.jsonl", broker_capture={
            "source": "unavailable", "capture_error": True})
    assert decision.status is ReconciliationStatus.HOLD
    assert decision.fail_closed is True
    assert json.loads(state.read_text())["positions"] == old


def test_t20_every_mismatch_emits_divergence_audit_and_alert(tmp_path):
    audit, alerts = tmp_path / "audit.json", tmp_path / "alerts.jsonl"
    broker = [_leg("TMFI6")]
    reconcile_holding(
        broker_legs=broker, local_legs=[], tick_size=1.0,
        audit_path=audit, alert_path=alerts, broker_capture=_capture(broker),
        local_source_path="/tmp/local-state.json")
    event = json.loads(audit.read_text())[-1]
    assert event["event"] == "BROKER_STATE_DIVERGENCE"
    assert event["broker_legs"] == broker and event["local_legs"] == []
    assert event["canonical_input_hash"] == "a" * 64
    assert json.loads(alerts.read_text().splitlines()[-1])["event"] == "BROKER_STATE_DIVERGENCE"


def test_t21_local_state_is_backed_up_before_convergence(tmp_path):
    state = tmp_path / "state.json"
    old = {"positions": [_leg("TMFI6")], "marker": "现场"}
    state.write_text(json.dumps(old, ensure_ascii=False))
    reconcile_holding(
        broker_legs=[], local_legs=old["positions"], tick_size=1.0,
        state_path=state, audit_path=tmp_path / "audit.json",
        alert_path=tmp_path / "alerts.jsonl", broker_capture=_capture([]))
    backups = list(tmp_path.glob("state.json.bak.*"))
    assert backups and json.loads(backups[0].read_text()) == old
