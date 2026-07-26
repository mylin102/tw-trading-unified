# 2026-07-26 Gemini CLI: Unit tests for PolicyJTelemetryWriter
from pathlib import Path
import json
import pytest

from strategies.futures.mts.policy_j_telemetry_schema import (
    EligibilityReason,
    PolicyJShadowSnapshot,
)
from strategies.futures.mts.policy_j_telemetry_writer import PolicyJTelemetryWriter


def test_telemetry_writer_append_and_isolation(tmp_path):
    writer = PolicyJTelemetryWriter(export_dir=tmp_path)
    snapshot = PolicyJShadowSnapshot(
        snapshot_id="SNAP_TEST_001",
        sequence_no=1,
        trade_id="TRADE_TEST",
        eligible=True,
        eligibility_reason=EligibilityReason.HEDGED_PAIR_SPREAD.value,
        gross_liquidation_pnl_twd=400.0,
    )

    success = writer.append_snapshot(snapshot, date_str="20260726")
    assert success is True
    assert writer.records_written == 1
    assert writer.write_error_count == 0

    target_file = tmp_path / "policy_j_shadow_20260726.jsonl"
    assert target_file.exists()

    content = target_file.read_text(encoding="utf-8").strip()
    data = json.loads(content)
    assert data["snapshot_id"] == "SNAP_TEST_001"
    assert data["gross_liquidation_pnl_twd"] == 400.0


def test_telemetry_writer_error_isolation(tmp_path):
    # Pass an invalid directory path that causes write failure
    invalid_path = tmp_path / "non_existent_file.txt"
    # Create file instead of directory so mkdir fails
    invalid_path.touch()

    writer = PolicyJTelemetryWriter(export_dir=invalid_path / "sub")
    snapshot = PolicyJShadowSnapshot(
        snapshot_id="SNAP_FAIL_001",
        trade_id="TRADE_FAIL",
    )

    # Invariant: Exception MUST be caught internally without raising to caller!
    success = writer.append_snapshot(snapshot, date_str="20260726")
    assert success is False
    assert writer.write_error_count == 1
