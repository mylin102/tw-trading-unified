"""2026-10-05 Codex: execute the actual dashboard FLAT UPL callsite.

Only Streamlit output is replaced; production branch control, snapshot
reader and session validation execute against isolated real-shaped files.
"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.performance_provenance import (
    broker_snapshot_live_upl, live_upl_presented_reason, upl_presentation)


def _flat_upl_callsite():
    path = Path(__file__).resolve().parents[2] / "ui" / "dashboard.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.If)
               and ast.unparse(node.test) == "_upl_pres['kind'] == 'ZERO'"]
    assert len(matches) == 1, "Execute exactly the production FLAT UPL branch"
    return compile(ast.Module(body=matches, type_ignores=[]), str(path), "exec")


@pytest.mark.parametrize("paper,scope_ok,expected", [
    (True, True, "0 TWD"), (True, False, "N/A"), (False, False, "N/A")])
def test_dashboard_flat_upl_is_mode_scoped(tmp_path, monkeypatch, paper, scope_ok, expected):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "execution_context.json").write_text(json.dumps({
        "session_id": "current-paper-session", "requested_mode": "paper" if paper else "live",
        "audit_reasons": []}))
    diagnostics = tmp_path / "exports" / "trades" / "live" / "diagnostics"
    diagnostics.mkdir(parents=True)
    (diagnostics / "broker_snapshot_canonical.json").write_text(json.dumps({
        "source": "live_broker", "mode": "live", "session_id": "old-live-session",
        "account_identity_hash": "account-hash", "canonical_input_hash": "input-hash",
        "captured_at": 1790905674995, "snapshot": {"positions": []}}))
    metrics, warnings, reads = [], [], []

    def read_snapshot(path):
        reads.append(str(path))
        return broker_snapshot_live_upl(path)

    scope = {"ok": scope_ok, "mode": "paper" if paper else "live",
             "reason": "paper view: missing execution evidence — cannot attribute"}
    metric = SimpleNamespace(metric=lambda label, value: metrics.append((label, value)))
    namespace = {
        "_upl_pres": upl_presentation(scope, True, None),
        "active_runtime_truth": {"is_paper_runtime": paper},
        "_u1": metric, "_u2": metric, "_u3": metric,
        "st": SimpleNamespace(warning=warnings.append),
        "broker_snapshot_live_upl": read_snapshot,
        "live_upl_presented_reason": live_upl_presented_reason,
        "runtime_path": lambda *parts: str(tmp_path.joinpath(*parts)),
        "Path": Path, "json": json,
    }
    exec(_flat_upl_callsite(), namespace)
    assert [value for _, value in metrics] == [expected] * 3
    if paper:
        assert reads == [], "PAPER display must never read the LIVE broker artifact"
        assert all("SESSION_MISMATCH" not in message for message in warnings)
        if not scope_ok:
            assert len(warnings) == 1
            assert scope["reason"] in warnings[0]
    else:
        assert len(reads) == 1
        assert len(warnings) == 1
        assert "SESSION_MISMATCH" in warnings[0]
