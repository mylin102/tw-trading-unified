"""Fail-closed contract of the single-command deploy script (Collapse 1).

The tests drive the REAL scripts/deployment/deploy_release.sh against a REAL
scripts/ci/contract_impact.py inside a throwaway git repository, so the IMPACT
stage is exercised end-to-end (audit 2026-09-20 round 3: a fixture that stubs
the classifier proves nothing about the deployed integration).

Only `pm2` and the stage helper scripts are stubbed; `git` is the real one and
the throwaway repo carries real commits for the "running" and "deployed" shas.
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
RELEASE_SCOPE_SRC = REPO_SRC / "scripts" / "deployment" / "release_scope.py"
IMPACT_SRC = REPO_SRC / "scripts" / "ci" / "contract_impact.py"


def _write(path: Path, text: str, exec_bit: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if exec_bit:
        path.chmod(0o755)


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _stub_bin(tmp: Path, running_sha: str = "BASE",
              pm2_start_status: str = "online", node_ok: bool = True) -> tuple[Path, Path]:
    """Return (bin_dir, pm2_log).  `running_sha` is resolved at layout time."""
    bin_dir = tmp / "stubbin"
    pm2_log = tmp / "pm2_calls.log"
    _write(bin_dir / "node", f"""#!/usr/bin/env bash
printf 'node %s\\n' "$*" >> {tmp / 'node_calls.log'}
if printf '%s' "$*" | grep -q 'process.stdout.write'; then
  printf '%s' '[{{"name":"trading-system","cwd":"{tmp / 'repo'}","script":"taskpolicy","env":{{"LRC_RELEASE_SHA":"candidate"}}}}]'
fi
exit {0 if node_ok else 1}
""", exec_bit=True)
    _write(bin_dir / "pm2", f"""#!/usr/bin/env bash
printf 'pm2 %s\\n' "$*" >> {pm2_log}
printf 'ENV LRC=%s NODE=%s PY=%s\\n' "$LRC_RELEASE_SHA" "$NODE_ENV" "$TRADING_PYTHON_BIN" >> {pm2_log}
case "$1" in
  delete|save) exit 0 ;;
  jlist)
    if grep -q "pm2 start" {pm2_log} 2>/dev/null; then st={pm2_start_status}; else st=stopped; fi
    if grep -q "pm2 start" {pm2_log} 2>/dev/null; then sha="$LRC_RELEASE_SHA"; else sha="{running_sha}"; fi
    printf '[{{"name":"trading-system","pm2_env":{{"status":"%s","LRC_RELEASE_SHA":"%s","pm_cwd":"{tmp / 'repo'}"}}}}]' "$st" "$sha"
    ;;
  stop|start) exit 0 ;;
esac
exit 0
""", exec_bit=True)
    return bin_dir, pm2_log


def _layout(tmp: Path, *, gate_pre_rc: int = 0, gate_post_rc: int = 0,
            reconcile_rc: int = 0, verify_rc: int = 0, disk_ok: bool = True,
            reconcile_unavailable: bool = False,
            env_ok: bool = True, scope_record: bool = False,
            evidence_fail: bool = False,
            deployed_change: str = "tests") -> Path:
    """Build a real git repo: base commit (running) + deployed commit (HEAD)."""
    repo = tmp / "repo"
    (repo / "scripts" / "deployment").mkdir(parents=True)
    shutil.copy2(DEPLOY_SRC, repo / "scripts" / "deployment" / "deploy_release.sh")
    (repo / "scripts" / "deployment" / "deploy_release.sh").chmod(0o755)
    shutil.copy2(RELEASE_SCOPE_SRC, repo / "scripts" / "deployment" / "release_scope.py")
    (repo / "scripts" / "ci").mkdir(parents=True, exist_ok=True)
    shutil.copy2(IMPACT_SRC, repo / "scripts" / "ci" / "contract_impact.py")
    (repo / "PHASE1_FINAL_FREEZE.md").write_text("manifest\n")
    _write(repo / "ecosystem.config.js",
           "module.exports = { apps: [{ name: 'trading-system', script: 'taskpolicy',"
           " args: '-c background main.py --config futures_live', interpreter: 'none',"
           " cwd: __dirname, autorestart: true, stop_exit_codes: [0] }] };\n")
    stub = "import sys; sys.exit(%d)\n"
    _write(repo / "scripts" / "deployment" / "re_freeze.py", "import sys; sys.exit(0)\n")
    _write(repo / "scripts" / "deployment" / "check_deployment.py",
           f"""import sys
open({str(tmp / "gate_calls.log")!r}, "a").write(" ".join(sys.argv) + "\\n")
if "--phase" in sys.argv and sys.argv[sys.argv.index("--phase") + 1] == "pre_deploy":
    sys.exit({gate_pre_rc})
sys.exit({gate_post_rc})
""")
    _write(repo / "scripts" / "deployment" / "capture_flat_evidence.py",
           f"""import json, os, sys
from pathlib import Path
rt = Path(os.environ["TRADING_RUNTIME_DIR"])
art = rt / "exports" / "trades" / "live" / "diagnostics" / "broker_snapshot_canonical.json"
marker = rt / "evidence_capture_ran"
marker.write_text("ran")
if {int(evidence_fail)}:
    _out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    if _out:
        Path(_out).write_text(json.dumps({{"preflight_passed": False, "artifact": None,
                                           "failed_checks": ["BROKER_CAPTURE_UNAVAILABLE"]}}))
    sys.exit(5)
art.parent.mkdir(parents=True, exist_ok=True)
art.write_text(json.dumps({{"source": "live_broker", "positions": [], "open_orders": [],
                            "session_scope": "read_only_capture"}}))
out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
payload = json.dumps({{"preflight_passed": True, "artifact": str(art), "failed_checks": []}})
if out:
    Path(out).write_text(payload)
print("broker banner noise on stdout")
print(payload)
""")
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
    _write(repo / "scripts" / "reconcile_pending_orders.py",
           "import os, sys\n"
           f"open({str(tmp / 'reconcile_env.log')!r}, 'w').write(os.environ.get('PYTHONPATH', ''))\n"
           f"print('reconcile_pending_orders: ' + ('BROKER_PROBE_UNAVAILABLE' if {int(reconcile_unavailable)} else 'broker evidence OK; 0 changes'))\n"
           f"sys.exit({reconcile_rc})\n")
    _write(repo / "core" / "order_intent_gateway.py", "GATEWAY = 1\n")
    _write(repo / "config" / "futures_live.yaml",
           "live_trading: true\nconfig_profile: futures_live\n")
    _write(repo / "tests" / "core" / "test_x.py", "def test_x():\n    assert True\n")
    _write(repo / "tests" / "core" / "test_order_intent_gateway.py", "def test_g():\n    assert True\n")
    (tmp / "runtime" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp / "runtime" / "execution_context.json").write_text(
        json.dumps({"requested_mode": "live", "effective_mode": "live_ready",
                    "live_order_allowed": True, "audit_reasons": [],
                    "session_id": "sess-1"}))
    (tmp / ".env").write_text("EXPORTED=1\n" if env_ok else "false\n")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (tmp / "base_sha").write_text(base_sha)

    # the deployed diff: TEST_ONLY (FAST) or a contract path (FULL)
    if deployed_change == "tests":
        _write(repo / "tests" / "core" / "test_x.py",
               "def test_x():\n    assert True  # deployed\n")
    else:
        _write(repo / "core" / "order_intent_gateway.py", "GATEWAY = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "deployed")
    head_sha = _git(repo, "rev-parse", "HEAD")
    (tmp / "head_sha").write_text(head_sha)

    if scope_record:
        (tmp / "runtime" / "logs" / "verify_scope.json").write_text(
            json.dumps({"sha": head_sha, "decision": "FULL"}))
    return repo


def _run(tmp: Path, repo: Path, bin_dir: Path, args: list[str] | None = None,
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    env["TRADING_RUNTIME_DIR"] = str(tmp / "runtime")
    env["TRADING_ENV_FILE"] = str(tmp / ".env")
    env["TRADING_PYTHON_BIN"] = sys.executable
    env.update(extra_env or {})
    return subprocess.run(["bash", str(repo / "scripts" / "deployment" / "deploy_release.sh"),
                           *(args or [])], capture_output=True, text=True, env=env,
                          cwd=str(repo), timeout=300)


def test_script_and_classifier_present():
    assert DEPLOY_SRC.exists(), "scripts/deployment/deploy_release.sh missing"
    assert IMPACT_SRC.exists(), "scripts/ci/contract_impact.py missing (deploy branch must carry it)"


def test_no_deploy_override_seam_in_production_script():
    text = DEPLOY_SRC.read_text(encoding="utf-8")
    for banned in ("DEPLOY_GIT_BIN", "DEPLOY_PM2_BIN", "DEPLOY_PYTHON_BIN",
                   "DEPLOY_MIN_FREE_GB", "DEPLOY_MANIFEST", "DEPLOY_POSITION_STATE",
                   "DEPLOY_PID_FILE", "DEPLOY_STOP_WAIT_S", "DEPLOY_START_WAIT_S",
                   "PYTEST_CURRENT_TEST", "stub"):
        assert banned not in text, f"production seam/knob found: {banned}"


def test_dry_run_prints_plan_only_and_touches_nothing(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path)
    base = (tmp_path / "base_sha").read_text().strip()
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=base)
    logs_before = set(os.listdir(tmp_path / "runtime" / "logs"))
    r = _run(tmp_path, repo, bin_dir, args=["--dry-run"])
    assert r.returncode == 0, r.stdout + r.stderr
    for st in ("PREFLIGHT", "FREEZE", "IMPACT", "EVIDENCE", "GATE_PRE", "STOP",
               "RECONCILE", "START", "GATE_POST", "VERIFY"):
        assert st in r.stdout, st
    assert "NOTHING was verified" in r.stdout, r.stdout
    assert not pm2_log.exists()
    assert set(os.listdir(tmp_path / "runtime" / "logs")) == logs_before


def test_dry_run_never_sources_the_env_file(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path)
    marker = tmp_path / "env_was_sourced"
    (tmp_path / ".env").write_text(f"touch {marker}\n")
    r = _run(tmp_path, repo, bin_dir, args=["--dry-run"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists(), "dry-run sourced the credentials file"
    assert not (tmp_path / "runtime" / "evidence_capture_ran").exists(), \
        "dry-run performed the broker evidence capture"
    assert not pm2_log.exists()


def test_test_only_diff_is_fast_and_proceeds(tmp_path):
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="tests")
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[DEPLOY OK]" in r.stdout
    log = pm2_log.read_text()
    assert "pm2 stop trading-system" in log
    assert "pm2 start ecosystem.config.js --only trading-system" in log


def test_contract_diff_requires_recorded_verification(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="contract")
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "stage=IMPACT" in r.stderr, r.stderr
    log = pm2_log.read_text() if pm2_log.exists() else ""
    assert "pm2 stop" not in log


def test_contract_diff_with_recorded_verification_proceeds(tmp_path):
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="contract", scope_record=True)
    bin_dir, _ = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[DEPLOY OK]" in r.stdout


def test_stale_verification_record_is_rejected(tmp_path):
    """A FULL record for a DIFFERENT sha must not authorise this deploy."""
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="contract",
                   scope_record=False)
    (tmp_path / "runtime" / "logs" / "verify_scope.json").write_text(
        json.dumps({"sha": "0" * 40, "decision": "FULL"}))
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=IMPACT" in r.stderr


def test_unknown_running_sha_forces_full(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha="")
    repo = _layout(tmp_path, deployed_change="tests")  # would be FAST
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "stage=IMPACT" in r.stderr, r.stderr
    log = pm2_log.read_text() if pm2_log.exists() else ""
    assert "pm2 stop" not in log


def test_same_sha_redeploy_follows_the_gate_empty_diff_is_full(tmp_path):
    """No NO_CHANGE shortcut: an empty diff is FULL, exactly like the gate says."""
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="tests")
    head = (tmp_path / "head_sha").read_text().strip()
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=head)
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "stage=IMPACT" in r.stderr
    log = pm2_log.read_text() if pm2_log.exists() else ""
    assert "pm2 stop" not in log


def test_env_load_failure_aborts_before_start(tmp_path):
    """A failing credentials file must abort before the app is ever started.

    Credentials are loaded before RECONCILE (the reconciliation probe needs
    them), so a broken .env now aborts there — still strictly before START.
    """
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, env_ok=False)
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "stage=PREFLIGHT" in r.stderr, r.stderr
    assert "credentials env load failed" in r.stderr, r.stderr
    log = pm2_log.read_text() if pm2_log.exists() else ""
    assert "pm2 start" not in log
    assert "pm2 stop" not in log, "a credentials failure must not stop a running app"


def test_gate_pre_failure_aborts_before_stop(tmp_path):
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, gate_pre_rc=1)
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=GATE_PRE" in r.stderr
    assert "pm2 stop" not in (pm2_log.read_text() if pm2_log.exists() else "")


def test_reconcile_failure_aborts_before_start(tmp_path):
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, reconcile_rc=1)
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=RECONCILE" in r.stderr
    log = pm2_log.read_text() if pm2_log.exists() else ""
    assert "pm2 stop" in log and "pm2 start" not in log


def test_gate_post_failure_reports_and_fails(tmp_path):
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, gate_post_rc=1)
    bin_dir, _ = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=GATE_POST" in r.stderr


def test_verify_failure_fails_closed(tmp_path):
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, verify_rc=4)
    bin_dir, _ = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=VERIFY" in r.stderr


def test_dirty_tree_aborts_at_preflight(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path)
    (repo / "core" / "order_intent_gateway.py").write_text("GATEWAY = 99  # uncommitted\n")
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=PREFLIGHT" in r.stderr
    assert not pm2_log.exists() or "pm2 stop" not in pm2_log.read_text()


def test_disk_floor_aborts_at_preflight(tmp_path):
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, disk_ok=False)
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0
    assert "stage=PREFLIGHT" in r.stderr
    assert not pm2_log.exists() or "pm2 stop" not in pm2_log.read_text()


def test_verifier_requires_expected_sha(tmp_path):
    import subprocess as _sp
    vlc = REPO_SRC / "scripts" / "deployment" / "verify_live_context.py"
    r = _sp.run([sys.executable, "-B", str(vlc), "--runtime-dir",
                 str(tmp_path / "runtime")], capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--expected-sha is required" in r.stdout


def test_pre_gate_receives_its_prerequisites(tmp_path):
    """The gate must get the sealed profile, its hash and the fresh flat evidence."""
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="tests")
    bin_dir, _ = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (tmp_path / "gate_calls.log").read_text().strip().splitlines()
    pre = next(c for c in calls if "pre_deploy" in c)
    assert "--config-profile config/futures_live.yaml" in pre, pre
    assert "--config-hash " in pre and len(pre.split("--config-hash ")[1].split()[0]) == 64, pre
    art = str(tmp_path / "runtime" / "exports" / "trades" / "live" / "diagnostics"
              / "broker_snapshot_canonical.json")
    assert f"--position-state {art}" in pre, pre
    assert f"--margin-evidence {art}" in pre, pre
    assert (tmp_path / "runtime" / "evidence_capture_ran").exists(), "evidence capture did not run"


def test_evidence_capture_failure_aborts_before_stop(tmp_path):
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="tests", evidence_fail=True)
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "stage=EVIDENCE" in r.stderr, r.stderr
    assert "pm2 stop" not in (pm2_log.read_text() if pm2_log.exists() else "")


def test_deploy_helpers_get_the_repo_on_pythonpath(tmp_path):
    """Real-machine finding: reconcile_pending_orders.py needs PYTHONPATH=repo."""
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="tests")
    bin_dir, _ = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = (tmp_path / "reconcile_env.log").read_text()
    assert str(repo) in seen, f"PYTHONPATH did not carry the repo: {seen!r}"


def test_start_re_registers_with_the_new_identity_pin(tmp_path):
    """The app must be started from the ecosystem file with THIS sha pinned."""
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="tests")
    head = (tmp_path / "head_sha").read_text().strip()
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode == 0, r.stdout + r.stderr
    log = pm2_log.read_text()
    assert "pm2 delete trading-system" in log
    assert "pm2 start ecosystem.config.js --only trading-system" in log
    assert "pm2 save" in log
    assert "restart" not in log, "pm2 restart is a forbidden deploy action"
    assert "--update-env" not in log, "--update-env is forbidden"
    env_lines = [l for l in log.splitlines() if l.startswith("ENV ")]
    assert env_lines, log
    assert f"LRC={head}" in env_lines[-1], env_lines[-1]
    assert "NODE=production" in env_lines[-1], env_lines[-1]


def test_bad_ecosystem_config_aborts_before_anything_is_stopped(tmp_path):
    """A PM2 config that rejects this release dir must abort BEFORE the stop."""
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="tests")
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip(),
                                 node_ok=False)
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "stage=PREFLIGHT" in r.stderr, r.stderr
    assert "ecosystem.config.js rejects this release dir/env" in r.stderr, r.stderr
    log = pm2_log.read_text() if pm2_log.exists() else ""
    assert "pm2 stop" not in log, "the app was stopped before the PM2 config was validated"
    assert "pm2 delete" not in log
    assert "pm2 start" not in log
    assert (tmp_path / "node_calls.log").exists(), "the ecosystem config was never validated"


def test_reconcile_refuses_unverified_pending_orders(tmp_path):
    """A probe-less reconcile is a no-op; the deploy must not start on it."""
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="tests", reconcile_unavailable=True)
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "stage=RECONCILE" in r.stderr, r.stderr
    log = pm2_log.read_text() if pm2_log.exists() else ""
    assert "pm2 start" not in log


def test_evidence_stage_propagates_the_capture_rc(tmp_path):
    """The EVIDENCE stage must surface the capture helper's own rc (5)."""
    bin_dir, _ = _stub_bin(tmp_path, running_sha="PLACEHOLDER")
    repo = _layout(tmp_path, deployed_change="tests", evidence_fail=True)
    bin_dir, pm2_log = _stub_bin(tmp_path, running_sha=(tmp_path / "base_sha").read_text().strip())
    r = _run(tmp_path, repo, bin_dir)
    assert r.returncode == 5, f"expected the helper rc 5, got {r.returncode}: {r.stdout}{r.stderr}"
    assert "stage=EVIDENCE rc=5" in r.stderr, r.stderr
    assert "pm2 stop" not in (pm2_log.read_text() if pm2_log.exists() else "")
