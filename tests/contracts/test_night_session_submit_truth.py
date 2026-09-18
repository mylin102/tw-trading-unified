"""Night-session safety contracts: broker submission is the truth boundary."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TMF = ROOT / "strategies/plugins/futures/active/tmf_spread.py"
MONITOR = ROOT / "strategies/futures/monitor.py"
MAIN = ROOT / "main.py"


def _method_calls(source: str, method_name: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
            return [n for n in ast.walk(node) if isinstance(n, ast.Call)]
    raise AssertionError(f"missing method: {method_name}")


def test_entry_state_and_submitted_event_are_deferred_until_broker_submit():
    source = TMF.read_text(encoding="utf-8")
    calls = _method_calls(source, "on_bar")
    state_writes = [c for c in calls if isinstance(c.func, ast.Name)
                    and c.func.id == "_write_mts_state"]
    submitted_events = [c for c in calls if isinstance(c.func, ast.Name)
                        and c.func.id == "_append_event"
                        and any(isinstance(a, ast.Constant)
                                and a.value == "ENTRY_SUBMITTED"
                                for a in c.args)]
    assert not state_writes, "entry must not persist leg state before broker confirmation"
    assert not submitted_events, "ENTRY_SUBMITTED must be emitted after broker acceptance"


def test_release_submitted_event_is_not_emitted_by_pre_submit_strategy_evaluation():
    source = TMF.read_text(encoding="utf-8")
    calls = _method_calls(source, "_manage_position")
    for call in calls:
        if not (isinstance(call.func, ast.Name) and call.func.id == "_append_event"):
            continue
        text = ast.unparse(call)
        assert "SUBMITTED" not in text, (
            "release SUBMITTED telemetry must be emitted only by the broker-success path"
        )


def test_reconnect_has_p1_evidence_hook():
    source = MAIN.read_text(encoding="utf-8")
    calls = _method_calls(source, "_try_shioaji_reconnect")
    assert any(isinstance(c.func, ast.Attribute)
               and c.func.attr == "_append_mts_event" for c in calls), (
        "session reconnect must leave an auditable P1 event"
    )


def test_broker_submit_success_path_owns_submission_events_and_state():
    source = MONITOR.read_text(encoding="utf-8")
    assert "ENTRY_SUBMITTED" in source
    assert "RELEASE_NEAR_SUBMITTED" in source
    assert "RELEASE_FAR_SUBMITTED" in source
