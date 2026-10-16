"""RED: deployment gate presentation consolidation.

Every underlying fail-closed guard and refusal code is preserved; the
top-level result consolidates into exactly four aggregate groups:
RELEASE_INTEGRITY / RUNTIME_READY / BROKER_TRUTH / STARTUP_AUTHORIZATION.
Presentation-only — authorization semantics, TTL and query-on-demand
are untouched.
"""
import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _result(guard, ok=True, reasons=()):
    from core.deployment_safety_gate import _fail, _pass
    if ok:
        return _pass(guard)
    return _fail(guard, list(reasons))


def test_aggregate_groups_exact_membership():
    """The twelve guards map exactly once into the four groups."""
    from core.deployment_safety_gate import GATE_AGGREGATE_GROUPS

    assert GATE_AGGREGATE_GROUPS == {
        "RELEASE_INTEGRITY": ("release_head", "clean_tree",
                              "rollback_manifest", "config_profile"),
        "RUNTIME_READY": ("runtime_paths", "single_process",
                          "ctx_atomic_health", "quarantine_first_startup"),
        "BROKER_TRUTH": ("flat_snapshot", "margin", "capture_consistency"),
        "STARTUP_AUTHORIZATION": ("session_generation",),
    }
    _all = sorted(g for gs in GATE_AGGREGATE_GROUPS.values() for g in gs)
    assert _all == sorted([
        "release_head", "clean_tree", "rollback_manifest", "config_profile",
        "runtime_paths", "single_process", "ctx_atomic_health",
        "quarantine_first_startup", "flat_snapshot", "margin",
        "capture_consistency", "session_generation"])


def test_aggregate_status_pass_and_refused():
    """A group is PASS only when all its guards pass; any refusal makes
    it REFUSED with the typed refusal codes, and the underlying guard
    diagnostics stay intact."""
    from core.deployment_safety_gate import aggregate_gate_groups

    _passing = [_result(g) for g in (
        "release_head", "clean_tree", "rollback_manifest", "config_profile")]
    _agg = aggregate_gate_groups(_passing)
    assert _agg["RELEASE_INTEGRITY"]["status"] == "PASS"
    assert _agg["RELEASE_INTEGRITY"]["refusals"] == []

    _failing = [_result("release_head"),
                _result("clean_tree", ok=False,
                        reasons=["GUARD_TREE_DIRTY"])]
    _agg2 = aggregate_gate_groups(_failing)
    assert _agg2["RELEASE_INTEGRITY"]["status"] == "REFUSED"
    assert _agg2["RELEASE_INTEGRITY"]["refusals"] == ["GUARD_TREE_DIRTY"]
    assert _agg2["RELEASE_INTEGRITY"]["guards"]["clean_tree"]["ok"] is False
    assert _agg2["RELEASE_INTEGRITY"]["guards"]["clean_tree"][
        "reasons"] == ["GUARD_TREE_DIRTY"]


def test_startup_authorization_not_assessed_pre_deploy_assessed_post_startup():
    """STARTUP_AUTHORIZATION (session_generation) is NOT_ASSESSED in
    pre_deploy and assessed (PASS/REFUSED) in post_startup."""
    from core.deployment_safety_gate import aggregate_gate_groups

    _ok = [_result("session_generation")]
    assert aggregate_gate_groups(
        _ok, phase="pre_deploy")["STARTUP_AUTHORIZATION"]["status"] == \
        "NOT_ASSESSED"
    assert aggregate_gate_groups(
        _ok, phase="post_startup")["STARTUP_AUTHORIZATION"]["status"] == \
        "PASS"
    _bad = [_result("session_generation", ok=False,
                    reasons=["GUARD_SESSION_INTERVENED"])]
    _post = aggregate_gate_groups(_bad, phase="post_startup")
    assert _post["STARTUP_AUTHORIZATION"]["status"] == "REFUSED"
    assert _post["STARTUP_AUTHORIZATION"]["refusals"] == \
        ["GUARD_SESSION_INTERVENED"]


def _run_cli(extra):
    return subprocess.run(
        [sys.executable,
         str(_REPO / "scripts/deployment/check_deployment.py"),
         "--release-dir", str(_REPO),
         "--pid-file", "/tmp/nonexistent-gate.pid",
         "--position-state", "/tmp/nonexistent-pos.json",
         "--phase", "pre_deploy"] + extra,
        capture_output=True, text=True, timeout=180,
        cwd=str(_REPO))


def test_cli_json_output_has_aggregates():
    """The CLI JSON output carries the four aggregate groups plus the
    per-guard diagnostics."""
    _r = _run_cli(["--json"])
    _data = json.loads(_r.stdout)
    assert set(_data["aggregates"]) == {
        "RELEASE_INTEGRITY", "RUNTIME_READY", "BROKER_TRUTH",
        "STARTUP_AUTHORIZATION"}
    assert _data["aggregates"]["STARTUP_AUTHORIZATION"]["status"] == \
        "NOT_ASSESSED"
    assert any(g["guard"] == "release_head" for g in _data["guards"])
    assert "refusal_codes" in _data


def test_cli_text_output_has_aggregate_lines():
    """The CLI text output prints the four GATE aggregate lines and
    keeps the per-guard diagnostics."""
    _r = _run_cli([])
    for _g in ("RELEASE_INTEGRITY", "RUNTIME_READY", "BROKER_TRUTH",
               "STARTUP_AUTHORIZATION"):
        assert f"GATE {_g}" in _r.stdout
    assert "NOT_ASSESSED" in _r.stdout
    assert ("[PASS] release_head" in _r.stdout
            or "[FAIL] release_head" in _r.stdout)
