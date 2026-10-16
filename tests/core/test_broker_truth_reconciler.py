import json
from pathlib import Path

import pytest

from core.broker_truth_reconciler import BrokerTruthConsensus


def _capture(positions, ts, digest):
    return {
        "source": "live_broker",
        "positions": positions,
        "open_orders": [],
        "captured_at": ts,
        "canonical_input_hash": digest,
    }


def _state(positions):
    return {"status": "ENTRY_RECONCILED" if positions else "FLAT",
            "positions": positions}


def test_t1_single_empty_does_not_flatten(tmp_path):
    old = [{"code": "TMFJ6", "qty": 1, "direction": "buy"}]
    r = BrokerTruthConsensus(tmp_path / "state.json", tmp_path / "audit.json")
    result = r.observe(_capture([], 1000, "a" * 64), _state(old), now=1000)
    assert result.consensus is False
    assert result.state["positions"] == old
    assert r.exit_allowed("TMFJ6") is False


def test_t2_two_consistent_empty_captures_flatten_and_audit(tmp_path):
    state_path, audit_path = tmp_path / "state.json", tmp_path / "audit.json"
    r = BrokerTruthConsensus(state_path, audit_path, required_observations=2,
                             min_interval_seconds=60)
    old = _state([{"code": "TMFJ6", "qty": 1, "direction": "buy"}])
    r.observe(_capture([], 1000, "a" * 64), old, now=1000,
              capture_path="cap-1.json")
    result = r.observe(_capture([], 1060, "b" * 64), old, now=1060,
                       capture_path="cap-2.json")
    assert result.consensus is True
    assert result.state["status"] == "FLAT"
    assert result.state["reconciled_from"] == "broker_query_consensus"
    event = json.loads(audit_path.read_text())[-1]
    assert {c["canonical_input_hash"] for c in event["captures"]} == {"a" * 64, "b" * 64}
    assert state_path.exists()


def test_t3_flap_resets_without_reconcile(tmp_path):
    r = BrokerTruthConsensus(tmp_path / "state.json", tmp_path / "audit.json",
                             required_observations=2, min_interval_seconds=60)
    old = _state([{"code": "TMFJ6", "qty": 1, "direction": "buy"}])
    r.observe(_capture([], 1000, "a" * 64), old, now=1000)
    r.observe(_capture(old["positions"], 1060, "b" * 64), old, now=1060)
    result = r.observe(_capture([], 1120, "c" * 64), old, now=1120)
    assert result.consensus is False
    assert result.state["positions"] == old["positions"]
    assert any(e["event"] == "BROKER_TRUTH_FLAP" for e in json.loads((tmp_path / "audit.json").read_text()))


def test_t4_broker_holding_reconciles_from_flat(tmp_path):
    r = BrokerTruthConsensus(tmp_path / "state.json", tmp_path / "audit.json")
    broker = [{"code": "TMFJ6", "qty": 1, "direction": "buy", "avg_cost": 46698}]
    old = _state([])
    r.observe(_capture(broker, 1000, "a" * 64), old, now=1000)
    result = r.observe(_capture(broker, 1060, "b" * 64), old, now=1060)
    assert result.consensus is True
    assert result.state["positions"] == broker


def test_t5_exit_blocked_until_consensus(tmp_path):
    r = BrokerTruthConsensus(tmp_path / "state.json", tmp_path / "audit.json")
    old = _state([{"code": "TMFJ6", "qty": 1, "direction": "buy"}])
    r.observe(_capture([], 1000, "a" * 64), old, now=1000)
    assert r.exit_allowed("TMFJ6") is False
    r.observe(_capture([], 1060, "b" * 64), old, now=1060)
    assert r.exit_allowed("TMFJ6") is False  # broker is now flat


def test_t6_state_and_audit_are_traceable_to_capture_files(tmp_path):
    r = BrokerTruthConsensus(tmp_path / "state.json", tmp_path / "audit.json")
    old = _state([{"code": "TMFJ6", "qty": 1, "direction": "buy"}])
    r.observe(_capture([], 1000, "a" * 64), old, now=1000, capture_path="one.json")
    r.observe(_capture([], 1060, "b" * 64), old, now=1060, capture_path="two.json")
    saved = json.loads((tmp_path / "state.json").read_text())
    event = json.loads((tmp_path / "audit.json").read_text())[-1]
    assert saved["capture_paths"] == ["one.json", "two.json"]
    assert [c["captured_at"] for c in event["captures"]] == [1000, 1060]


def test_t7_threshold_is_configurable_but_cannot_be_one(tmp_path):
    with pytest.raises(ValueError):
        BrokerTruthConsensus(tmp_path / "s", tmp_path / "a", required_observations=1)
    r = BrokerTruthConsensus(tmp_path / "s", tmp_path / "a",
                             required_observations=3, min_interval_seconds=5)
    assert r.required_observations == 3
