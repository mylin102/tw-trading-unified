#!/usr/bin/env python3
"""Step 6 — execution-context persistence (research/core wiring only).

Contracts (live_route_certification_phase2 §8.4 / §9.3):
- atomic write: tmp + flush + fsync(file) + os.replace + fsync(parent)
- read fail-closed: missing -> LIVE_QUARANTINED (RESTART_MAINTAIN_QUARANTINE);
  corrupt / schema-invalid -> LIVE_QUARANTINED (STATE_FILE_CORRUPTED)
- persistence failure must NEVER enable LIVE (reader is file-based)
- schema: requested_mode, effective_mode, live_order_allowed,
  audit_reasons (finite strings), revision, updated_at, + safe
  session/certificate/lifecycle fields (no secrets)
"""

import json

import pytest


def _ctx_dict(**over):
    d = {
        "requested_mode": "live",
        "effective_mode": "live_quarantined",
        "live_order_allowed": False,
        "audit_reasons": ["RECONNECT_HANDOFF"],
        "account_id_hash": "abc123",
        "session_id": "s-1",
        "process_start_id": "p-1",
        "config_hash": "cfg-1",
        "state_namespace": "live",
    }
    d.update(over)
    return d


def test_module_exists_red():
    # RED: the persistence module is the design contract (§8.4)
    import core.execution_context_state  # noqa: F401


def test_write_read_round_trip(tmp_path):
    import core.execution_context_state as st
    st.persist_execution_context(_ctx_dict(), runtime_dir=str(tmp_path))
    data = st.read_execution_context(runtime_dir=str(tmp_path))
    assert data["effective_mode"] == "live_quarantined"
    assert data["audit_reasons"] == ["RECONNECT_HANDOFF"]
    assert data["live_order_allowed"] is False
    assert isinstance(data["revision"], int) and data["revision"] >= 1
    assert data["updated_at"], "updated_at must be set"


def test_missing_file_fail_closed(tmp_path):
    import core.execution_context_state as st
    data = st.read_execution_context(runtime_dir=str(tmp_path))
    assert data["effective_mode"] == "live_quarantined"
    assert data["live_order_allowed"] is False
    assert "RESTART_MAINTAIN_QUARANTINE" in data["audit_reasons"], data


def test_corrupt_file_fail_closed(tmp_path):
    import core.execution_context_state as st
    p = tmp_path / "execution_context.json"
    p.write_text("{not json!!", encoding="utf-8")
    data = st.read_execution_context(runtime_dir=str(tmp_path))
    assert data["effective_mode"] == "live_quarantined"
    assert data["live_order_allowed"] is False
    assert "STATE_FILE_CORRUPTED" in data["audit_reasons"], data


def test_schema_invalid_fail_closed(tmp_path):
    import core.execution_context_state as st
    p = tmp_path / "execution_context.json"
    p.write_text(json.dumps({"effective_mode": "live_ready",  # missing keys
                             "audit_reasons": "not-a-list"}),
                 encoding="utf-8")
    data = st.read_execution_context(runtime_dir=str(tmp_path))
    assert data["effective_mode"] == "live_quarantined"
    assert data["live_order_allowed"] is False
    assert "STATE_FILE_CORRUPTED" in data["audit_reasons"], data


def test_atomic_write_failure_fail_closed(tmp_path, monkeypatch):
    # a failed atomic write must never tear/corrupt the target; the
    # reader keeps the LAST GOOD state (never enabled LIVE)
    import core.execution_context_state as st
    st.persist_execution_context(_ctx_dict(), runtime_dir=str(tmp_path))
    good = st.read_execution_context(runtime_dir=str(tmp_path))

    def _boom_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("os.replace", _boom_replace)
    with pytest.raises(OSError):
        st.persist_execution_context(
            _ctx_dict(effective_mode="live_ready", live_order_allowed=True),
            runtime_dir=str(tmp_path))
    monkeypatch.undo()
    # file untouched -> reader still reflects the last good (quarantined) state
    after = st.read_execution_context(runtime_dir=str(tmp_path))
    assert after == good
    assert after["effective_mode"] == "live_quarantined"
    assert after["live_order_allowed"] is False


def test_transition_update_revision(tmp_path):
    # every transition bumps revision and the reader reflects the LATEST
    import core.execution_context_state as st
    st.persist_execution_context(_ctx_dict(), runtime_dir=str(tmp_path))
    r1 = st.read_execution_context(runtime_dir=str(tmp_path))
    st.persist_execution_context(
        _ctx_dict(effective_mode="live_ready", live_order_allowed=True,
                  audit_reasons=[]),
        runtime_dir=str(tmp_path))
    r2 = st.read_execution_context(runtime_dir=str(tmp_path))
    assert r2["revision"] == r1["revision"] + 1
    assert r2["effective_mode"] == "live_ready"
    assert r2["live_order_allowed"] is True


def test_restart_round_trip(tmp_path):
    # "restart" = a fresh read call after the process would have died;
    # effective_mode/audit_reasons must be identical
    import core.execution_context_state as st
    st.persist_execution_context(
        _ctx_dict(effective_mode="live_ready", live_order_allowed=True,
                  audit_reasons=["CERT_OK"]),
        runtime_dir=str(tmp_path))
    data = st.read_execution_context(runtime_dir=str(tmp_path))
    assert data["effective_mode"] == "live_ready"
    assert data["audit_reasons"] == ["CERT_OK"]
    assert data["live_order_allowed"] is True


def test_exit_only_capability_round_trips_restart_safe(tmp_path):
    import core.execution_context_state as st
    capability = {
        "reconciliation_id": "recon-1",
        "allowed_orders": [{"symbol": "TMFH6", "side": "buy", "remaining_qty": 1}],
    }
    st.persist_execution_context(_ctx_dict(
        effective_mode="reconciled_exit_only",
        exit_only_capability=capability,
    ), runtime_dir=str(tmp_path))
    data = st.read_execution_context(runtime_dir=str(tmp_path))
    assert data["effective_mode"] == "reconciled_exit_only"
    assert data["live_order_allowed"] is False
    assert data["exit_only_capability"]["reconciliation_id"] == "recon-1"


def test_exit_only_without_valid_capability_fails_closed(tmp_path):
    import core.execution_context_state as st
    payload = _ctx_dict(
        effective_mode="reconciled_exit_only",
        exit_only_capability="not-a-capability",
        revision=1,
        updated_at="2026-08-11T00:00:00Z",
    )
    (tmp_path / "execution_context.json").write_text(json.dumps(payload))
    data = st.read_execution_context(runtime_dir=str(tmp_path))
    assert data["effective_mode"] == "live_quarantined"
    assert "STATE_FILE_CORRUPTED" in data["audit_reasons"]
