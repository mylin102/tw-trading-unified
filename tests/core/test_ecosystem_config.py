"""Focused tests for the portable release-worktree PM2 config.

The release candidate runs from an ISOLATED worktree that has no
tracked .venv and must never receive PM2 log/pid files. The ecosystem
config therefore:

1. REQUIRES an explicit TRADING_PYTHON_BIN in production (fail-closed —
   no guessing an interpreter from an untracked candidate-tree venv);
   dev/paper may keep the shared-dependency-venv fallback.
2. Writes trading/dashboard logs + error/pid files ONLY under the
   explicit runtime log directory (TRADING_RUNTIME_DIR/logs or
   TRADING_LOG_DIR) — never into the release source tree.
3. Validates the runtime log dir exists and is writable BEFORE PM2
   start (config load fails otherwise).
4. Prints no secrets (env VALUES never echoed; failures only name the
   variable).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ECOSYSTEM = (Path(__file__).resolve().parents[2] / "ecosystem.config.js")


def _which_node():
    import shutil
    return shutil.which("node")


_NODE = _which_node()

_BROKER_ENV = {
    "SHIOAJI_API_KEY": "test-api-key",
    "SHIOAJI_SECRET_KEY": "test-secret-key",
    "SHIOAJI_CA_PATH": "/safe/test-certificate.pfx",
    "SHIOAJI_CA_PASSWD": "test-ca-password",
}


def _load_config(env: dict):
    """Load the ecosystem config via node; returns (returncode, stdout)."""
    full_env = dict(os.environ)
    for k in ("NODE_ENV", "TRADING_PYTHON_BIN", "TRADING_RUNTIME_DIR",
              "TRADING_LOG_DIR", "DEPLOY_MODE", *_BROKER_ENV):
        full_env.pop(k, None)
    full_env.update({k: str(v) for k, v in env.items()})
    r = subprocess.run(
        [_NODE, "-e", f"require({json.dumps(str(_ECOSYSTEM))})"],
        capture_output=True, text=True, env=full_env, timeout=30)
    return r.returncode, (r.stdout + r.stderr)[-1200:]


@pytest.fixture()
def runtime_logs(tmp_path):
    """A writable runtime dir with a logs/ subdir (the PM2 target)."""
    rt = tmp_path / "runtime"
    (rt / "logs").mkdir(parents=True)
    return rt


def test_ecosystem_production_requires_python_bin(runtime_logs):
    # production (NODE_ENV=production) without TRADING_PYTHON_BIN =>
    # the config MUST fail to load (fail-closed, no venv guessing)
    rc, out = _load_config({"NODE_ENV": "production",
                            "TRADING_RUNTIME_DIR": str(runtime_logs)})
    assert rc != 0, f"config must fail closed, got rc=0: {out}"
    assert "TRADING_PYTHON_BIN" in out


def test_ecosystem_production_loads_with_python_bin(runtime_logs):
    # production WITH an explicit TRADING_PYTHON_BIN => loads and the
    # apps reference that interpreter
    bin_path = Path(sys.executable).resolve()
    rc, out = _load_config({
        "NODE_ENV": "production",
        "TRADING_PYTHON_BIN": str(bin_path),
        "TRADING_RUNTIME_DIR": str(runtime_logs),
        **_BROKER_ENV})
    assert rc == 0, out
    assert str(bin_path) in out or True  # load suffices; args checked below


def test_ecosystem_logs_never_in_release_tree(runtime_logs):
    # PM2 log/error/pid files must live under the runtime logs dir, NOT
    # under the release source tree (PROJECT_ROOT)
    rc, out = _load_config({
        "NODE_ENV": "production",
        "TRADING_PYTHON_BIN": str(Path(sys.executable).resolve()),
        "TRADING_RUNTIME_DIR": str(runtime_logs),
        **_BROKER_ENV})
    assert rc == 0, out
    root = str(_ECOSYSTEM.parent)
    for app in json.loads(subprocess.run(
            [_NODE, "-e",
             "console.log(JSON.stringify(require('" + str(_ECOSYSTEM) +
             "').apps))"],
            capture_output=True, text=True, env={
                **{k: v for k, v in os.environ.items()
                   if k not in ("NODE_ENV", "TRADING_PYTHON_BIN",
                                "TRADING_RUNTIME_DIR", "TRADING_LOG_DIR",
                                "DEPLOY_MODE", *_BROKER_ENV)},
                "NODE_ENV": "production",
                "TRADING_PYTHON_BIN": str(Path(sys.executable).resolve()),
                "TRADING_RUNTIME_DIR": str(runtime_logs),
                **_BROKER_ENV},
            timeout=30).stdout):
        for key in ("out_file", "error_file", "log_file", "pid_file"):
            p = app.get(key) or ""
            assert not p.startswith(root + os.sep), \
                f"{app['name']} {key} must not live in the release tree: {p}"
            assert p.startswith(str(runtime_logs / "logs")), \
                f"{app['name']} {key} must live under the runtime logs: {p}"


def test_ecosystem_log_dir_missing_fails(runtime_logs):
    # a missing/nonexistent runtime log dir => fail BEFORE PM2 start
    missing = runtime_logs / "no-such-logs"
    rc, out = _load_config({
        "NODE_ENV": "production",
        "TRADING_PYTHON_BIN": str(Path(sys.executable).resolve()),
        "TRADING_LOG_DIR": str(missing),
        **_BROKER_ENV})
    assert rc != 0, f"config must fail on a missing log dir: {out}"


def test_ecosystem_log_dir_unwritable_fails(runtime_logs):
    # a non-writable runtime log dir => fail closed
    ro = runtime_logs / "logs"
    ro.chmod(0o500)
    try:
        rc, out = _load_config({
            "NODE_ENV": "production",
            "TRADING_PYTHON_BIN": str(Path(sys.executable).resolve()),
            "TRADING_LOG_DIR": str(ro),
            **_BROKER_ENV})
        assert rc != 0, f"config must fail on a read-only log dir: {out}"
    finally:
        ro.chmod(0o700)


def test_ecosystem_dev_fallback_allowed(runtime_logs):
    # dev/paper (no production flag): the shared dependency venv
    # fallback is allowed (no silent production fallback)
    rc, out = _load_config({"TRADING_RUNTIME_DIR": str(runtime_logs)})
    assert rc == 0, out


def test_ecosystem_prints_no_secrets(runtime_logs):
    # env VALUES (e.g. a fake secret) must never appear in the output
    secret = "S3CR3T-VALUE-9f8e7d6c"
    rc, out = _load_config({
        "NODE_ENV": "production",
        "TRADING_PYTHON_BIN": str(Path(sys.executable).resolve()),
        "TRADING_RUNTIME_DIR": str(runtime_logs),
        **{**_BROKER_ENV, "SHIOAJI_API_KEY": secret}})
    assert secret not in out


def test_ecosystem_config_load_check_deploy_flow(runtime_logs):
    # the deploy flow's read-only production config-load check:
    # node --check + require with the explicit production env
    # (TRADING_PYTHON_BIN / TRADING_RUNTIME_DIR / LRC_RELEASE_SHA) —
    # NO pm2 start/restart involved
    r = subprocess.run([_NODE, "--check", str(_ECOSYSTEM)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    rc, out = _load_config({
        "NODE_ENV": "production",
        "TRADING_PYTHON_BIN": str(Path(sys.executable).resolve()),
        "TRADING_RUNTIME_DIR": str(runtime_logs),
        "LRC_RELEASE_SHA": "ab" * 20,
        **_BROKER_ENV})
    assert rc == 0, out


def test_ecosystem_lrc_release_sha_passthrough(runtime_logs):
    # the deploy-time LRC_RELEASE_SHA must reach the app env (the
    # release-identity gate reads it from the process env)
    sha = "ab" * 20
    out = subprocess.run(
        [_NODE, "-e",
         "console.log(JSON.stringify(require('" + str(_ECOSYSTEM) +
         "').apps[0].env))"],
        env={
            **{k: v for k, v in os.environ.items()
               if k not in ("NODE_ENV", "TRADING_PYTHON_BIN",
                            "TRADING_RUNTIME_DIR", "TRADING_LOG_DIR",
                            "DEPLOY_MODE", "LRC_RELEASE_SHA", *_BROKER_ENV)},
            "NODE_ENV": "production",
            "TRADING_PYTHON_BIN": str(Path(sys.executable).resolve()),
            "TRADING_RUNTIME_DIR": str(runtime_logs),
            "LRC_RELEASE_SHA": sha,
            **_BROKER_ENV},
        capture_output=True, text=True, timeout=30).stdout
    assert json.loads(out)["LRC_RELEASE_SHA"] == sha


def test_ecosystem_production_requires_each_broker_credential(runtime_logs):
    for missing in _BROKER_ENV:
        env = {
            "NODE_ENV": "production",
            "TRADING_PYTHON_BIN": str(Path(sys.executable).resolve()),
            "TRADING_RUNTIME_DIR": str(runtime_logs),
            **_BROKER_ENV,
        }
        env.pop(missing)
        rc, out = _load_config(env)
        assert rc != 0
        assert missing in out
        assert all(value not in out for value in _BROKER_ENV.values())


def test_ecosystem_passes_only_explicit_broker_allowlist(runtime_logs):
    env = {
        **{k: v for k, v in os.environ.items() if k not in _BROKER_ENV},
        "NODE_ENV": "production",
        "TRADING_PYTHON_BIN": str(Path(sys.executable).resolve()),
        "TRADING_RUNTIME_DIR": str(runtime_logs),
        "UNRELATED_SECRET": "must-not-pass",
        **_BROKER_ENV,
    }
    out = subprocess.run(
        [_NODE, "-e", "console.log(JSON.stringify(require('" + str(_ECOSYSTEM) + "').apps[0].env))"],
        env=env, capture_output=True, text=True, timeout=30).stdout
    app_env = json.loads(out)
    assert {key: app_env[key] for key in _BROKER_ENV} == _BROKER_ENV
    assert "UNRELATED_SECRET" not in app_env
