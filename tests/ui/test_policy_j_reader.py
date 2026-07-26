# 2026-07-26 Gemini CLI: Unit tests for PolicyJTelemetryReader
from pathlib import Path
import json
import pytest

from ui.services.policy_j_reader import PolicyJTelemetryReader


def test_policy_j_reader_load_snapshots(tmp_path):
    jsonl_file = tmp_path / "policy_j_shadow_20260726.jsonl"
    jsonl_file.write_text(
        '{"schema_version": "1.1", "sequence_no": 1, "trade_id": "T1", "estimated_net_exit_pnl_twd": 200.0}\n'
        '{"schema_version": "1.1", "sequence_no": 2, "trade_id": "T1", "estimated_net_exit_pnl_twd": 500.0}\n'
        '{"schema_version": "2.0", "sequence_no": 3, "trade_id": "T1", "estimated_net_exit_pnl_twd": 600.0}\n'  # Major 2.0 -> Rejected
        'CORRUPTED_LINE_TAIL...\n',
        encoding="utf-8"
    )

    reader = PolicyJTelemetryReader(export_dir=tmp_path)
    dates = reader.list_available_session_dates()
    assert "20260726" in dates

    snapshots = reader.load_snapshots("20260726", trade_id="T1")
    assert len(snapshots) == 2
    assert snapshots[0]["sequence_no"] == 1
    assert snapshots[1]["sequence_no"] == 2
    assert snapshots[1]["estimated_net_exit_pnl_twd"] == 500.0
