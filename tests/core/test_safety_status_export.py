import json

import core.channel_safety as channel_safety
from core.channel_safety import (
    AccountDegradedReason,
    ChannelSafetyState,
    read_persisted_safety_snapshot,
)


def test_published_snapshot_reports_reconciliation_pending(tmp_path, monkeypatch):
    status_path = tmp_path / "safety.json"
    monkeypatch.setattr(channel_safety, "SAFETY_STATUS_PATH", status_path)

    state = ChannelSafetyState()
    state.set_account_degraded(
        AccountDegradedReason.RECONCILIATION_PENDING,
        "STARTUP_BROKER_QUERY_FAILED: ServerError",
    )

    payload = read_persisted_safety_snapshot(status_path)
    assert payload is not None
    assert payload["entry_allowed"] is False
    assert payload["entry_blocked_reason"] == "RECONCILIATION_PENDING"
    assert payload["account_degraded_message"].startswith("STARTUP_BROKER_QUERY_FAILED")
    assert payload["updated_at"] > 0
    assert json.loads(status_path.read_text()) == payload


def test_published_snapshot_reports_entry_unlocked_only_after_reconciliation(tmp_path, monkeypatch):
    status_path = tmp_path / "safety.json"
    monkeypatch.setattr(channel_safety, "SAFETY_STATUS_PATH", status_path)

    state = ChannelSafetyState()
    state.set_account_healthy()
    state.set_reconciled()

    payload = read_persisted_safety_snapshot(status_path)
    assert payload is not None
    assert payload["entry_allowed"] is True
    assert payload["entry_blocked_reason"] is None
