import json
from pathlib import Path

import pytest

from core.shioaji_session import (
    audit_resident_shioaji_constructors,
    observe_api_created,
    observe_broker_call,
    observe_login,
    observe_logout,
)


class FakeAPI:
    futopt_account = type("Account", (), {"account_id": "ACC-1"})()


def _events(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_session_lifecycle_events_have_generation_identity_and_last_success(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    monkeypatch.setenv("MTS_SESSION_EVENT_LOG_PATH", str(path))
    api = FakeAPI()
    created = observe_api_created(api, source="core.shioaji_session.get_api")
    observe_login(api, source="core.shioaji_session._login", success=True)
    observe_logout(api, source="core.shioaji_session.logout")
    events = _events(path)
    assert [event["event"] for event in events] == [
        "SESSION_API_CREATED", "SESSION_LOGIN", "SESSION_LOGOUT"
    ]
    assert created["session_generation"] == events[0]["session_generation"]
    assert events[1]["last_successful_login_generation"] == created["session_generation"]
    for event in events:
        assert event["pid"] > 0
        assert event["thread_name"]
        assert event["api_object_id"] == id(api)
        assert event["account"] == "ACC-1"
        assert event["source"]
        assert event["ts"]


def test_broker_call_logs_before_after_and_failure_generation(tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    monkeypatch.setenv("MTS_SESSION_EVENT_LOG_PATH", str(path))
    api = FakeAPI()
    observe_api_created(api, source="test.create")
    observe_login(api, source="test.login", success=True)
    observe_broker_call(api, "list_positions", "before", source="main.verify")
    observe_broker_call(api, "list_positions", "after", source="main.verify")
    observe_broker_call(
        api, "update_status", "error", source="main.verify",
        error=RuntimeError("SessionNotEstablished"))
    events = _events(path)
    calls = [event for event in events if event["event"] == "BROKER_CALL"]
    assert [event["phase"] for event in calls] == ["before", "after", "error"]
    assert all(event["session_generation"] == calls[0]["session_generation"] for event in calls)
    assert calls[-1]["last_successful_login_generation"] == calls[0]["session_generation"]
    assert calls[-1]["error_type"] == "RuntimeError"


def test_paper_two_session_owners_are_observable_without_broker_login(tmp_path, monkeypatch):
    path = tmp_path / "two-owners.jsonl"
    monkeypatch.setenv("MTS_SESSION_EVENT_LOG_PATH", str(path))
    first, second = FakeAPI(), FakeAPI()
    observe_api_created(first, source="paper.owner_a")
    observe_login(first, source="paper.owner_a", success=True)
    observe_api_created(second, source="paper.owner_b")
    observe_login(second, source="paper.owner_b", success=True)
    events = [event for event in _events(path) if event["event"] == "SESSION_LOGIN"]
    assert len(events) == 2
    assert {event["api_object_id"] for event in events} == {id(first), id(second)}
    assert events[0]["session_generation"] != events[1]["session_generation"]


def test_resident_constructor_audit_is_clean_and_probe_file_is_rejected(tmp_path):
    repo = Path(__file__).parents[2]
    resident = ["main.py", "scripts/runners/stock_runner.py", "strategies/stocks"]
    assert audit_resident_shioaji_constructors(repo, resident) == []
    probe = tmp_path / "scripts" / "runners" / "probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text("import shioaji as sj\napi = sj.Shioaji()\n")
    violations = audit_resident_shioaji_constructors(tmp_path, ["scripts/runners"])
    assert len(violations) == 1
    assert violations[0]["path"] == "scripts/runners/probe.py"
