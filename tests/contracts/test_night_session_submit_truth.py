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
        assert "RELEASE_NEAR_SUBMITTED" not in text
        assert "RELEASE_FAR_SUBMITTED" not in text, (
            "release SUBMITTED telemetry must be emitted only by the broker-success path"
        )


def test_reconnect_has_p1_evidence_hook():
    source = MAIN.read_text(encoding="utf-8")
    calls = _method_calls(source, "_try_shioaji_reconnect")
    assert any(isinstance(c.func, ast.Name)
               and c.func.id == "_reconnect_evidence" for c in calls), (
        "session reconnect must leave an auditable P1 event"
    )


def test_broker_submit_success_path_owns_submission_events_and_state():
    source = MONITOR.read_text(encoding="utf-8")
    assert "ENTRY_SUBMITTED" in source
    assert "RELEASE_NEAR_SUBMITTED" in source
    assert "RELEASE_FAR_SUBMITTED" in source


def test_live_startup_latches_broker_truth_before_local_fills_restore():
    """A stale local fills ledger must not rebuild live state first."""
    tree = ast.parse(MONITOR.read_text(encoding="utf-8"))
    mts_tick = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_mts_tick"
    )
    refresh_lines = [
        node.lineno for node in ast.walk(mts_tick)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_refresh_live_broker_authority"
    ]
    restore_lines = [
        node.lineno for node in ast.walk(mts_tick)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_restore_from_fills_log"
    ]
    assert refresh_lines and restore_lines
    assert min(refresh_lines) < min(restore_lines), (
        "broker truth must be queried before fills-led startup recovery"
    )
