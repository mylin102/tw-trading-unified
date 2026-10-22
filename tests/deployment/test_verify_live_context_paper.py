"""Slice P: verify_live_context recognises a paper deployment.

Paper (requested=paper, effective=paper_active) is checked against paper rules:
live_order_allowed must be False, audit_reasons empty, session_id in the
timestamp format core/mode_transition.py writes.  Every other context keeps the
live rules, and a missing/corrupt context stays fail-closed.  The running pm2
identity check is the same in both modes.
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

VLC = Path(__file__).resolve().parents[2] / "scripts" / "deployment" / "verify_live_context.py"
SHA = "b8f46395" + "0" * 32
PAPER_SID = "20261022_092434"


def _ctx(**over):
    d = {"requested_mode": "paper", "effective_mode": "paper_active",
         "live_order_allowed": False, "audit_reasons": [],
         "session_id": PAPER_SID, "revision": 1}
    d.update(over)
    return d


def _live_ctx(**over):
    d = {"requested_mode": "live", "effective_mode": "live_ready",
         "live_order_allowed": True, "audit_reasons": [],
         "session_id": "a" * 32, "revision": 3}
    d.update(over)
    return d


def _fake_pm2(tmp_path, sha):
    script = tmp_path / "fake_pm2"
    env = {"LRC_RELEASE_SHA": sha}
    script.write_text(
        "#!/bin/sh\n"
        f"echo '{json.dumps([{'name': 'trading-system', 'pm2_env': env}])}'\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _run(tmp_path, ctx, *, sha=SHA, expected=SHA, raw=None):
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    ctx_file = runtime / "execution_context.json"
    if raw is not None:
        ctx_file.write_text(raw, encoding="utf-8")
    elif ctx is not None:
        ctx_file.write_text(json.dumps(ctx), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-B", str(VLC), "--runtime-dir", str(runtime),
         "--expected-sha", expected, "--pm2-bin", _fake_pm2(tmp_path, sha)],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))


def test_paper_context_ok(tmp_path):
    r = _run(tmp_path, _ctx())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[VERIFY] paper context OK" in r.stdout


def test_paper_with_live_order_allowed_fails(tmp_path):
    # the critical safety case: paper must never carry live order authority
    r = _run(tmp_path, _ctx(live_order_allowed=True))
    assert r.returncode != 0, r.stdout
    assert "paper context OK" not in r.stdout


def test_paper_with_live_order_allowed_missing_fails(tmp_path):
    d = _ctx()
    del d["live_order_allowed"]
    r = _run(tmp_path, d)
    assert r.returncode != 0, r.stdout


def test_paper_with_audit_reasons_fails(tmp_path):
    r = _run(tmp_path, _ctx(audit_reasons=["RESTART_MAINTAIN_QUARANTINE"]))
    assert r.returncode != 0, r.stdout


def test_paper_with_malformed_session_id_fails(tmp_path):
    for sid in (None, "", "2026-10-22", "a" * 32):
        r = _run(tmp_path, _ctx(session_id=sid))
        assert r.returncode != 0, (sid, r.stdout)


def test_paper_identity_mismatch_rc4(tmp_path):
    r = _run(tmp_path, _ctx(), sha="0" * 40, expected=SHA)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "identity mismatch" in r.stdout


def test_live_context_ok_regression(tmp_path):
    r = _run(tmp_path, _live_ctx())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[VERIFY] live context OK" in r.stdout


def test_live_identity_mismatch_rc4_regression(tmp_path):
    r = _run(tmp_path, _live_ctx(), sha="0" * 40, expected=SHA)
    assert r.returncode == 4, r.stdout + r.stderr


def test_live_quarantined_context_stays_fail_closed(tmp_path):
    d = _live_ctx(effective_mode="live_quarantined", live_order_allowed=False)
    r = _run(tmp_path, d)
    assert r.returncode == 3, r.stdout + r.stderr


def test_mixed_requested_paper_effective_quarantined_fails(tmp_path):
    d = _ctx(effective_mode="live_quarantined", live_order_allowed=False)
    r = _run(tmp_path, d)
    assert r.returncode == 3, r.stdout + r.stderr


def test_missing_or_corrupt_context_fails_closed(tmp_path):
    r = _run(tmp_path, None)
    assert r.returncode == 2, r.stdout
    r2 = _run(tmp_path, None, raw="{not json")
    assert r2.returncode == 2, r2.stdout
