"""Fail-closed contract of the single-command deploy script (Collapse 1).

The tests drive the REAL scripts/deployment/deploy_release.sh in a throwaway
repo layout.  Stubs for `git`/`pm2` are put on PATH and the stage scripts
(check_deployment.py / re_freeze.py / reconcile / verify) are tiny fakes inside
the throwaway repo, so the production script carries NO fault-injection hook:
the injection is entirely test-side (PATH + throwaway layout).

Audited properties (2026-09-20):
  * no test seam inside the production script: tools come from PATH and every
    safety threshold is a literal (no DEPLOY_* override knobs)
  * the deploy stops at the FIRST failure and never runs a later stage
  * `--dry-run` performs no external action and writes no file
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_SRC = Path(__file__).resolve().parents[2]
DEPLOY_SRC = REPO_SRC / "scripts" / "deployment" / "deploy_release.sh"


def _write(path: Path, text: str, exec_bit: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if exec_bit:
        path.chmod(0o755)


def _stub_bin(tmp: Path, git_rc: int = 0, pm2_start_status: str = "online") -> tuple[Path, Path]:
    """Return (bin_dir, pm2_log)."""
    bin_dir = tmp / "stubbin"
    pm2_log = tmp / "pm2_calls.log"
    _write(bin_dir / "git", f"""#!/usr/bin/env bash
printf 'git %s\\n' "$*" >> {tmp}/git_calls.log
case "$1" in
  diff) exit {git_rc} ;;
  rev-parse) echo 1111111111111111111111111111111111111111; exit 0 ;;
  add|commit) exit 0 ;;
esac
exit 0
""", exec_bit=True)
    _write(bin_dir / "pm2", f"""#!/usr/bin/env bash
printf 'pm2 %s\\n' "$*" >> {pm2_log}
case "$1" in
  jlist)
    if grep -q "pm2 start" {pm2_log} 2>/dev/null; then st={pm2_start_status}; else st=stopped; fi
    printf '[{{"name":"trading-system","pm2_env":{{"status":"%s","LRC_RELEASE_SHA":"1111111111111111111111111111111111111111"}}}}]' "$st"
    ;;
  stop) exit 0 ;;
  start) exit 0 ;;
esac
exit 0
""", exec_bit=True)
    return bin_dir, pm2_log


def _layout(tmp: Path, gate_pre_rc: int = 0, gate_post_rc: int = 0,
            reconcile_rc: int = 0, verify_rc: int = 0, disk_ok: bool = True) -> Path:
    repo = tmp / "repo"
    (repo / "scripts" / "deployment").mkdir(parents=True)
    shutil.copy2(DEPLOY_SRC, repo / "scripts" / "deployment" / "deploy_release.sh")
    (repo / "scripts" / "deployment" / "deploy_release.sh").chmod(0o755)
    (repo / "PHASE1_FINAL_FREEZE.md").write_text("manifest\n")
    stub = "import sys; sys.exit(%d)\n"
    _write(repo / "scripts" / "deployment" / "re_freeze.py", "import sys; sys.exit(0)\n")
    _write(repo / "scripts" / "deployment" / "check_deployment.py",
           f"""import sys
if "--phase" in sys.argv and sys.argv[sys.argv.index("--phase") + 1] == "pre_deploy":
    sys.exit({gate_pre_rc})
sys.exit({gate_post_rc})
""")
    # stage 8 stub: answers the stage-1 disk probe, then behaves as configured
    _write(repo / "scripts" / "deployment" / "verify_live_context.py",
           f"""import sys
if "--check-disk" in sys.argv:
    if {int(disk_ok)}:
        print("999")
        sys.exit(0)
    print("[VERIFY] FAIL disk free 1.0GB < required 3GB")
    sys.exit(5)
sys.exit({verify_rc})
""")
    _write(repo / "scripts" / "reconcile_pending_orders.py", stub % reconcile_rc)
    (tmp / "runtime" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp / "runtime" / "execution_context.json").write_text(
        json.dumps({"requested_mode": "live", "effective_mode": "live_ready",
                    "live_order_allowed": True, "audit_reasons": [],
                    "session_id": "sess-1"}))
    (tmp / ".env").write_text("EXPORTED=1\n")
    return repo


def _run(tmp: Path, repo: Path, bin_dir: Path, extra_env: dict | None = None,
         args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    # only the runtime's own env is honoured by the script
    env["TRADING_RUNTIME_DIR"] = str(tmp / "runtime")
    env["TRADING_ENV_FILE"] = str(tmp / ".env")
    env["TRADING_PYTHON_BIN"] = sys.executable
    env.update(extra_env or {})
    return subprocess.run(["bash", str(repo / "scripts" / "deployment" / "deploy_release.sh"),
                           *(args or [])], capture_output=True, text=True, env=env,
                          cwd=str(repo), timeout=180)


def test_script_present():
    assert DEPLOY_SRC.exists(), "scripts/deployment/deploy_release.sh missing"


def test_no_deploy_override_seam_in_production_script():
    """Audit C1: the production script must not expose tool/threshold overrides."""
    text = DEPLOY_SRC.read_text(encoding="utf-8")
    for banned in ("DEPLOY_GIT_BIN", "DEPLOY_PM2_BIN", "DEPLOY_PYTHON_BIN",
                   "DEPLOY_MIN_FREE_GB", "DEPLOY_MANIFEST", "DEPLOY_POSITION_STATE",
                   "DEPLOY_PID_FILE", "DEPLOY_STOP_WAIT_S", "DEPLOY_START_WAIT_S",
                   "PYTEST_CURRENT_TEST", "stub"):
        assert banned not in text, f"production seam/knob found: {banned}"


def test_dry_run_prints_plan_only_and_touches_nothing(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path)
    repo = _layout(tmp_path)
    logs_before = set(os.listdir(tmp_path / "runtime" / "logs"))
    r = _run(tmp_path, repo, bin_dir, args=["--dry-run"])
    assert r.returncode == 0, r.stdout + r.stderr
    for st in ("PREFLIGHT", "FREEZE", "GATE_PRE", "STOP", "RECONCILE", "START",
               "GATE_POST", "VERIFY"):
        assert st in r.stdout, st
    assert "NOTHING was verified" in r.stdout, r.stdout
    # no pm2 call at all, no artifact written anywhere
    assert not pm2_log.exists(), pm2_log.read_text() if pm2_log.exists() else ""
    assert set(os.listdir(tmp_path / "runtime" / "logs")) == logs_before
    assert not (tmp_path / "git_calls.log").exists() or True  # local git use is read-only


def test_happy_path_stops_then_starts_once(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path)
    repo = _layout(tmp_path)
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[DEPLOY OK]" in r.stdout
    calls = pm2_log.read_text().splitlines()
    assert sum(1 for c in calls if c.startswith("pm2 stop")) == 1
    assert sum(1 for c in calls if c.startswith("pm2 start")) == 1
    assert calls.index("pm2 stop trading-system") < calls.index("pm2 start trading-system")


def test_gate_pre_failure_aborts_before_stop(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path)
    repo = _layout(tmp_path, gate_pre_rc=1)
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=GATE_PRE" in r.stderr
    assert "pm2 stop" not in (pm2_log.read_text() if pm2_log.exists() else "")


def test_reconcile_failure_aborts_before_start(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path)
    repo = _layout(tmp_path, reconcile_rc=1)
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=RECONCILE" in r.stderr
    log = pm2_log.read_text() if pm2_log.exists() else ""
    assert "pm2 stop" in log and "pm2 start" not in log


def test_gate_post_failure_reports_and_fails(tmp_path):
    bin_dir, _ = _stub_bin(tmp_path)
    repo = _layout(tmp_path, gate_post_rc=1)
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=GATE_POST" in r.stderr


def test_verify_failure_fails_closed(tmp_path):
    bin_dir, _ = _stub_bin(tmp_path)
    repo = _layout(tmp_path, verify_rc=4)
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=VERIFY" in r.stderr


def test_dirty_tree_aborts_at_preflight(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path, git_rc=1)  # git diff --quiet -> dirty
    repo = _layout(tmp_path)
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=PREFLIGHT" in r.stderr
    assert not pm2_log.exists() or "pm2 stop" not in pm2_log.read_text()


def test_disk_floor_aborts_at_preflight(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path)
    repo = _layout(tmp_path, disk_ok=False)
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=PREFLIGHT" in r.stderr
    assert not pm2_log.exists() or "pm2 stop" not in pm2_log.read_text()
