"""RED: auditable redeploy bootstrap/reset for a stale execution_context.

The release5 pre_deploy was blocked by a stale runtime ctx
(SESSION_LOGOUT + a timestamp session_id from the STOPPED process).
The fix is a CONTROLLED reset flow (scripts/deployment/
redeploy_bootstrap.py):

- DRY-RUN by default; the actual reset requires --apply AND all guards
  PASS: no active process/pid, fresh live_broker futures-flat/no-orders
  snapshot, sealed live config profile, no pending safety reconcile,
  readable ctx in an acceptable stale state (SESSION_LOGOUT /
  RESTART_MAINTAIN_QUARANTINE). Anything else (corrupt ctx, reconcile
  pending, nonflat/pending, active pid, profile/snapshot mismatch)
  REFUSES and never touches the file.
- Apply archives the old ctx (full content + sha256 + timestamp) under
  <runtime>/logs/context_history/ then atomically rewrites the ctx as
  LIVE_QUARANTINED with audit_reasons=(REDEPLOY_BOOTSTRAP,) — NEVER
  LIVE_READY, never a session generation.
- pre_deploy accepts the bootstrap ctx (no standalone snapshot session
  comparison — the ctx session_id is None); post_startup STILL requires
  the registry generation/session match.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "deployment" / "redeploy_bootstrap.py"

_STALE_CTX = {
    "account_id_hash": None,
    "audit_reasons": ["SESSION_LOGOUT"],
    "config_hash": None,
    "effective_mode": "live_quarantined",
    "live_order_allowed": False,
    "process_start_id": "0e3367936f2dcfe9",
    "requested_mode": "paper",
    "revision": 1,
    "session_id": "20260810_070039",
    "state_namespace": "paper",
    "updated_at": "2026-08-09T23:02:25.294+00:00",
}


def _write_ctx(runtime_dir: Path, data: dict):
    p = runtime_dir / "execution_context.json"
    p.write_text(json.dumps(data), encoding="utf-8")


def _write_snapshot(tmp_path, **over):
    data = {"source": "live_broker", "mode": "live",
            "positions": [], "open_orders": [],
            "captured_at": int(time.time() * 1000),
            "canonical_input_hash": "b" * 64, "session_id": "1d9889aa",
            "account_identity_hash": "a" * 64}
    data.update(over)
    p = tmp_path / "preflight.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _write_profile(tmp_path):
    import hashlib
    p = tmp_path / "config" / "futures_live.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("live_trading: true\nconfig_profile: futures_live\n",
                 encoding="utf-8")
    return str(p), hashlib.sha256(p.read_bytes()).hexdigest()


def _run(repo: Path, *args, env_extra=None):
    env = dict(os.environ)
    for k in ("TRADING_RUNTIME_DIR", "LRC_RELEASE_SHA"):
        env.pop(k, None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-B", str(_SCRIPT), *map(str, args)],
        capture_output=True, text=True, env=env, timeout=60)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    rt = tmp_path / "runtime"
    (rt / "logs").mkdir(parents=True)
    _write_ctx(rt, _STALE_CTX)
    pf = _write_snapshot(tmp_path)
    prof, prof_hash = _write_profile(tmp_path)
    pid = tmp_path / "nope.pid"          # absent => no active process
    return {"rt": rt, "pf": pf, "prof": prof, "prof_hash": prof_hash,
            "pid": pid, "tmp": tmp_path}


def _base_args(e):
    return ["--runtime-dir", str(e["rt"]), "--pid-file", str(e["pid"]),
            "--position-state", str(e["pf"]), "--margin-evidence",
            str(e["pf"]), "--config-profile", str(e["prof"]),
            "--config-hash", e["prof_hash"]]


def test_dry_run_default_does_not_modify_ctx(env):
    r = _run(env["tmp"], *_base_args(env))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY_RUN" in r.stdout
    cur = json.loads((env["rt"] / "execution_context.json").read_text())
    assert cur == _STALE_CTX, "dry-run must never modify the ctx"


def test_apply_requires_all_guards_pid_active(env):
    (env["pid"]).write_text("999999999\n")     # active-looking pid
    r = _run(env["tmp"], *_base_args(env), "--apply")
    assert r.returncode != 0
    cur = json.loads((env["rt"] / "execution_context.json").read_text())
    assert cur == _STALE_CTX, "refused apply must not modify the ctx"


def test_apply_refuses_nonflat_snapshot(env):
    pf = _write_snapshot(env["tmp"], positions=[
        {"account": "futures", "code": "TMFH6", "quantity": 1,
         "direction": "Action.Sell"}])
    r = _run(env["tmp"], "--runtime-dir", str(env["rt"]),
             "--pid-file", str(env["pid"]), "--position-state", str(pf),
             "--margin-evidence", str(pf), "--config-profile",
             str(env["prof"]), "--config-hash", env["prof_hash"], "--apply")
    assert r.returncode != 0
    assert "flat" in (r.stdout + r.stderr).lower()
    assert json.loads((env["rt"] / "execution_context.json").read_text()) \
        == _STALE_CTX


def test_apply_refuses_reconcile_pending(env):
    _write_ctx(env["rt"], {**_STALE_CTX,
                           "audit_reasons": ["SAFETY_STOP_RECONCILE_PENDING"]})
    r = _run(env["tmp"], *_base_args(env), "--apply")
    assert r.returncode != 0
    assert "reconcile" in (r.stdout + r.stderr).lower()
    cur = json.loads((env["rt"] / "execution_context.json").read_text())
    assert "SAFETY_STOP_RECONCILE_PENDING" in cur["audit_reasons"]


def test_apply_refuses_corrupt_ctx(env):
    (env["rt"] / "execution_context.json").write_text("{not json",
                                                      encoding="utf-8")
    r = _run(env["tmp"], *_base_args(env), "--apply")
    assert r.returncode != 0
    assert (env["rt"] / "execution_context.json").read_text() == "{not json"


def test_apply_refuses_profile_mismatch(env):
    r = _run(env["tmp"], "--runtime-dir", str(env["rt"]),
             "--pid-file", str(env["pid"]), "--position-state",
             str(env["pf"]), "--margin-evidence", str(env["pf"]),
             "--config-profile", str(env["prof"]),
             "--config-hash", "cd" * 32, "--apply")
    assert r.returncode != 0
    assert json.loads((env["rt"] / "execution_context.json").read_text()) \
        == _STALE_CTX


def test_apply_archives_and_resets(env):
    r = _run(env["tmp"], *_base_args(env), "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    # archive: full content + sha256 + timestamp under logs/context_history
    hist = env["rt"] / "logs" / "context_history"
    files = sorted(hist.glob("*.json"))
    assert len(files) == 1, f"expected one archive, got {[f.name for f in files]}"
    archived = json.loads(files[0].read_text())
    assert archived["ctx"] == _STALE_CTX
    assert archived["sha256"] == hashlib.sha256(
        json.dumps(_STALE_CTX).encode()).hexdigest()
    assert archived["timestamp"]
    # ctx: LIVE_QUARANTINED + REDEPLOY_BOOTSTRAP + no session
    cur = json.loads((env["rt"] / "execution_context.json").read_text())
    assert cur["effective_mode"] == "live_quarantined"
    assert cur["live_order_allowed"] is False
    assert cur["audit_reasons"] == ["REDEPLOY_BOOTSTRAP"]
    assert not cur.get("session_id")
    assert cur.get("config_hash") == env["prof_hash"]


def test_predeploy_accepts_redeploy_bootstrap_ctx(env, monkeypatch):
    # after the reset, the pre_deploy gate accepts the bootstrap ctx: the
    # flat guard must NOT compare the standalone snapshot session (ctx
    # session_id is None) and ctx_atomic_health accepts REDEPLOY_BOOTSTRAP
    from core.deployment_safety_gate import guard_flat_no_pending, \
        guard_ctx_atomic_health
    ctx = {"audit_reasons": ["REDEPLOY_BOOTSTRAP"],
           "effective_mode": "live_quarantined", "live_order_allowed": False,
           "session_id": None, "config_hash": None}
    _write_ctx(env["rt"], ctx)
    r1 = guard_flat_no_pending(str(env["pf"]), ctx)
    assert r1.ok, r1.reasons
    r2 = guard_ctx_atomic_health(str(env["rt"]))
    assert r2.ok, r2.reasons


def test_post_startup_still_requires_generation(env, monkeypatch):
    # the bootstrap ctx does NOT weaken post_startup: the registry-bound
    # session generation is still mandatory
    from core.deployment_safety_gate import guard_session_generation
    r = guard_session_generation(None, False)
    assert not r.ok and "GUARD_SESSION_MISSING" in r.reasons
