import json
from pathlib import Path

from ui.broker_divergence_presentation import divergence_panel


def _event(ts=1000, disposition="converged_to_broker"):
    return {
        "event": "BROKER_STATE_DIVERGENCE", "captured_at": ts,
        "source": "periodic", "source_path": "/tmp/capture.json",
        "broker_legs": [{"code": "TMFI6", "side": "LONG", "qty": 1, "avg_cost": 100}],
        "local_legs": [{"code": "TMFI6", "side": "LONG", "qty": 2, "avg_cost": 99}],
        "difference": "LEG_IDENTITY_MISMATCH",
        "disposition": disposition, "audit_path": "/tmp/audit.jsonl",
    }


def test_t22_event_panel_has_all_fields(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps(_event()) + "\n")
    panel = divergence_panel(path, now=1000, max_age_seconds=60)
    assert panel["status"] == "OK"
    row = panel["events"][0]
    for key in ("timestamp", "source", "broker_legs", "local_legs",
                "difference", "disposition", "audit_path"):
        assert row[key] not in (None, "")
    assert row["broker_legs"][0]["code"] == "TMFI6"


def test_t23_no_event_is_explicit_na(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("")
    panel = divergence_panel(path, now=1000)
    assert panel["status"] == "N/A"
    assert panel["events"] == []


def test_t24_old_event_is_stale(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps(_event(ts=1)) + "\n")
    panel = divergence_panel(path, now=1000, max_age_seconds=60)
    assert panel["status"] == "STALE"
    assert panel["events"] == []


def test_t25_read_failure_is_explicit_error_without_old_rows(tmp_path):
    panel = divergence_panel(tmp_path / "missing.jsonl", now=1000)
    assert panel["status"] == "ERROR"
    assert panel["events"] == []
    assert panel["error"]


def test_t26_dashboard_reader_has_no_state_write_path():
    source = Path(__file__).resolve().parents[2] / "ui/broker_divergence_presentation.py"
    text = source.read_text()
    assert "write_text" not in text
    assert "os.replace" not in text
    assert "state_path" not in text
