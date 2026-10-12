"""2026-10-08 Codex: deployment orchestration must not report partial success."""
from types import SimpleNamespace

import pytest


class Operations:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __getattr__(self, stage):
        def call(*args):
            self.calls.append(stage)
            if stage == self.fail:
                raise RuntimeError(stage)
        return call


def run(ops, operation="release", apply=True):
    from scripts.deployment.deploy_mini import execute
    return execute(SimpleNamespace(operation=operation, apply=apply,
                                   maintenance_approved=True, mode="paper"), ops)


def test_release_real_orchestrator_orders_all_stages():
    ops = Operations()
    run(ops)
    assert ops.calls == ["preflight", "lock", "backup", "stop", "install",
                         "start", "observe", "save", "unlock", "complete"]


@pytest.mark.parametrize("stage", ["preflight", "backup", "stop", "install", "start", "observe", "save"])
def test_failed_release_never_unlocks_or_succeeds(stage):
    ops = Operations(fail=stage)
    with pytest.raises(RuntimeError, match=stage):
        run(ops)
    assert "unlock" not in ops.calls and "complete" not in ops.calls
    if stage == "observe":
        assert "save" not in ops.calls


def test_parameter_update_never_calls_pm2_or_moves_head():
    ops = Operations()
    run(ops, "params")
    assert ops.calls == ["preflight", "lock", "backup", "publish_params",
                         "observe", "unlock", "complete"]


def test_plan_is_read_only():
    ops = Operations()
    run(ops, apply=False)
    assert ops.calls == ["preflight", "plan"]


def test_live_cannot_enter_paper_workflow():
    from scripts.deployment.deploy_mini import execute, DeployError
    ops = Operations()
    with pytest.raises(DeployError, match="LIVE"):
        execute(SimpleNamespace(mode="live", operation="release", apply=True,
                                maintenance_approved=True), ops)
    assert ops.calls == []


def test_release_requires_explicit_maintenance_approval():
    from scripts.deployment.deploy_mini import execute, DeployError
    ops = Operations()
    with pytest.raises(DeployError, match="MAINTENANCE"):
        execute(SimpleNamespace(mode="paper", operation="release", apply=True,
                                maintenance_approved=False), ops)
    assert ops.calls == []


def test_pin_verifier_rejects_the_observed_stale_case(tmp_path):
    from scripts.deployment.deploy_mini import verify_app, DeployError
    row = {"name": "dashboard", "pid": 12, "pm2_env": {
        "pm_cwd": str(tmp_path), "TRADING_CANONICAL_ROOT": str(tmp_path),
        "status": "online", "LRC_RELEASE_SHA": "c" * 40,
        "TRADING_RUNTIME_DIR": str(tmp_path / "runtime"),
    }}
    with pytest.raises(DeployError, match="SHA"):
        verify_app(row, tmp_path, "9" * 40, tmp_path / "runtime")


def test_params_allow_only_width_change():
    from scripts.deployment.deploy_mini import updated_width, DeployError
    old = b"ticker: TMF\nlive_trading: false\nmts:\n  params:\n    max_spread_width: 3\n"
    new = updated_width(old, 4)
    import yaml
    assert yaml.safe_load(new)["mts"]["params"]["max_spread_width"] == 4
    assert yaml.safe_load(old)["mts"]["params"]["max_spread_width"] == 3
    for bad in (0, float("nan"), float("inf"), True):
        with pytest.raises((DeployError, ValueError)):
            updated_width(old, bad)
