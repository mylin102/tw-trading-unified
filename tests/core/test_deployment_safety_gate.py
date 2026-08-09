#!/usr/bin/env python3
"""Deployment Safety Gate — non-applied pre-deploy guard suite.

Ten guards + an aggregate fail-closed decision. The gate NEVER deploys,
NEVER restarts, NEVER unlocks LIVE — it only reports structured PASS/FAIL
reasons and refusal codes.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

SHA = "5" * 40
OTHER = "9" * 40
CLOSURE = ["config/futures.yaml", "main.py"]


# ── helpers ────────────────────────────────────────────────────────────────

def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email",
                    "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name",
                    "t"], check=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "futures.yaml").write_text(
        "mts:\n  live_required_margin_per_pair: 100000.0\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"],
                   check=True)
    return tmp_path


def _head(repo) -> str:
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _monitor_text_gated():
    return (
        "def __init__(self):\n"
        "    if self.live_trading:\n"
        "        self._execution_context = live_preflight_context()\n"
        "        _ = transition_with_certificate(None, None, None)\n"
        "def _place_safety_stop(self):\n"
        "    if not (self._execution_context and "
        "self._execution_context.is_live_ready()):\n"
        "        return {'blocked': True}\n"
        "    self.api.place_order(None)\n"
        "def _cancel_safety_stop(self):\n"
        "    if not (self._execution_context and "
        "self._execution_context.is_live_ready()):\n"
        "        return {'blocked': True}\n"
        "    self.api.cancel_order(None)\n"
        "def _execute_trade(self):\n"
        "    if not (self._execution_context and "
        "self._execution_context.is_live_ready()):\n"
        "        return {'blocked': True}\n"
        "    self.client.place_order(None)\n"
    )


def _ctx_file(runtime_dir, data):
    p = Path(runtime_dir)
    p.mkdir(parents=True, exist_ok=True)
    (p / "logs").mkdir(exist_ok=True)
    f = p / "execution_context.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def _ready_ctx_dict():
    return {"requested_mode": "live", "effective_mode": "live_ready",
            "live_order_allowed": True, "audit_reasons": [],
            "revision": 3, "updated_at": "2026-08-09T12:00:00Z",
            "account_id_hash": None, "session_id": None,
            "process_start_id": None, "config_hash": None,
            "state_namespace": "mts"}


# ── 1. release HEAD ────────────────────────────────────────────────────────

def test_guard_release_head_match(tmp_path, monkeypatch):
    from core.deployment_safety_gate import guard_release_head
    repo = _git_repo(tmp_path)
    monkeypatch.setenv("LRC_RELEASE_SHA", _head(repo))
    r = guard_release_head(str(repo))
    assert r.ok, r.reasons
    assert r.reasons == ()


def test_guard_release_head_env_missing(tmp_path, monkeypatch):
    from core.deployment_safety_gate import guard_release_head
    repo = _git_repo(tmp_path)
    monkeypatch.delenv("LRC_RELEASE_SHA", raising=False)
    r = guard_release_head(str(repo))
    assert not r.ok and "GUARD_HEAD_ENV_MISSING" in r.reasons


def test_guard_release_head_mismatch(tmp_path, monkeypatch):
    from core.deployment_safety_gate import guard_release_head
    repo = _git_repo(tmp_path)
    monkeypatch.setenv("LRC_RELEASE_SHA", OTHER)
    r = guard_release_head(str(repo))
    assert not r.ok and "GUARD_HEAD_MISMATCH" in r.reasons


def test_guard_release_head_cwd_independent(tmp_path, monkeypatch):
    # git -C <release_dir> — works from ANY cwd (release dir, not cwd)
    from core.deployment_safety_gate import guard_release_head
    repo = _git_repo(tmp_path)
    monkeypatch.setenv("LRC_RELEASE_SHA", _head(repo))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    r = guard_release_head(str(repo))
    assert r.ok, r.reasons


# ── 2. clean closure tree ──────────────────────────────────────────────────

def test_guard_clean_tree_clean(tmp_path):
    from core.deployment_safety_gate import guard_clean_tree
    repo = _git_repo(tmp_path)
    r = guard_clean_tree(str(repo), CLOSURE)
    assert r.ok, r.reasons


def test_guard_clean_tree_dirty(tmp_path):
    from core.deployment_safety_gate import guard_clean_tree
    repo = _git_repo(tmp_path)
    (repo / "config" / "futures.yaml").write_text("changed\n",
                                                  encoding="utf-8")
    r = guard_clean_tree(str(repo), CLOSURE)
    assert not r.ok and "GUARD_TREE_DIRTY" in r.reasons
    assert "futures.yaml" in r.detail


# ── 3. runtime paths ───────────────────────────────────────────────────────

def test_guard_runtime_paths_ok(tmp_path, monkeypatch):
    from core.deployment_safety_gate import guard_runtime_paths
    rt = tmp_path / "rt"
    _ctx_file(rt, _ready_ctx_dict())
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(rt))
    r = guard_runtime_paths(str(rt))
    assert r.ok, r.reasons


def test_guard_runtime_paths_env_missing(tmp_path, monkeypatch):
    from core.deployment_safety_gate import guard_runtime_paths
    monkeypatch.delenv("TRADING_RUNTIME_DIR", raising=False)
    r = guard_runtime_paths("")
    assert not r.ok and "GUARD_RUNTIME_ENV_MISSING" in r.reasons


def test_guard_runtime_paths_secret_scan(tmp_path):
    from core.deployment_safety_gate import guard_runtime_paths
    rt = tmp_path / "rt"
    _ctx_file(rt, {"api_key": "S3CR3T", "effective_mode": "live_ready"})
    r = guard_runtime_paths(str(rt))
    assert not r.ok and "GUARD_RUNTIME_SECRETS" in r.reasons


def test_guard_runtime_paths_not_writable(tmp_path):
    from core.deployment_safety_gate import guard_runtime_paths
    rt = tmp_path / "rt"
    _ctx_file(rt, _ready_ctx_dict())
    (rt / "logs").chmod(0o500)
    try:
        r = guard_runtime_paths(str(rt))
        assert not r.ok and "GUARD_RUNTIME_NOT_WRITABLE" in r.reasons
    finally:
        (rt / "logs").chmod(0o755)


# ── 4. single process / duplicate instance ─────────────────────────────────

def test_guard_single_process_alive(tmp_path):
    from core.deployment_safety_gate import guard_single_process
    pid_file = tmp_path / "app.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    r = guard_single_process(str(pid_file))
    assert not r.ok and "GUARD_DUPLICATE_INSTANCE" in r.reasons


def test_guard_single_process_stale_or_absent(tmp_path):
    from core.deployment_safety_gate import guard_single_process
    pid_file = tmp_path / "app.pid"
    pid_file.write_text("99999999", encoding="utf-8")  # dead pid
    r = guard_single_process(str(pid_file))
    assert r.ok, r.reasons
    r2 = guard_single_process(str(tmp_path / "nope.pid"))
    assert r2.ok, r2.reasons


# ── 5. flat / no pending order snapshot (provenance-aware) ─────────────────

def _position_file(runtime_dir, data):
    p = Path(runtime_dir) / "logs"
    p.mkdir(parents=True, exist_ok=True)
    f = p / "position_state.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return f


def _live_snapshot(**over):
    data = {"source": "live_broker", "mode": "live",
            "positions": 0, "open_orders": [],
            "captured_at": time.time(),          # fresh (epoch seconds)
            "hash": "a" * 40, "session_id": "sess-1"}
    data.update(over)
    return data


def _live_ctx_dict():
    d = _ready_ctx_dict()
    d["session_id"] = "sess-1"
    return d


def _paper_ctx_dict():
    return {"requested_mode": "paper", "effective_mode": "paper_active",
            "live_order_allowed": False, "audit_reasons": [],
            "revision": 1, "updated_at": "2026-08-09T12:00:00Z",
            "account_id_hash": None, "session_id": None,
            "process_start_id": None, "config_hash": None,
            "state_namespace": "mts"}


def test_guard_flat_live_broker_ok(tmp_path):
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, _live_snapshot())
    r = guard_flat_no_pending(str(pf), _live_ctx_dict())
    assert r.ok, r.reasons


def test_guard_flat_paper_holding_accepted_for_paper(tmp_path):
    # paper HOLDING_SPREAD is accepted ONLY for a paper deployment —
    # paper positions are allowed without claiming live readiness
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, {"source": "paper", "mode": "paper",
                             "has_position": True, "state": "HOLDING_SPREAD"})
    r = guard_flat_no_pending(str(pf), _paper_ctx_dict())
    assert r.ok, r.reasons
    assert "paper" in r.detail


def test_guard_flat_paper_never_satisfies_live(tmp_path):
    # paper snapshot is NEVER live-flat evidence
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, {"source": "paper", "mode": "paper",
                             "has_position": True, "state": "HOLDING_SPREAD"})
    r = guard_flat_no_pending(str(pf), _live_ctx_dict())
    assert not r.ok and "GUARD_SNAPSHOT_PAPER_NOT_LIVE" in r.reasons


def test_guard_flat_live_not_flat(tmp_path):
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, _live_snapshot(positions=2))
    r = guard_flat_no_pending(str(pf), _live_ctx_dict())
    assert not r.ok and "GUARD_POSITION_NOT_FLAT" in r.reasons


def test_guard_flat_live_pending_orders(tmp_path):
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, _live_snapshot(open_orders=["o1"]))
    r = guard_flat_no_pending(str(pf), _live_ctx_dict())
    assert not r.ok and "GUARD_PENDING_ORDERS" in r.reasons


def test_guard_flat_snapshot_missing(tmp_path):
    from core.deployment_safety_gate import guard_flat_no_pending
    r = guard_flat_no_pending(str(tmp_path / "nope.json"), _live_ctx_dict())
    assert not r.ok and "GUARD_SNAPSHOT_MISSING" in r.reasons


def test_guard_flat_source_missing_ambiguous(tmp_path):
    # no source/mode -> fail-closed (never assumed flat)
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, {"position": 0, "pending_orders": []})
    r = guard_flat_no_pending(str(pf), _live_ctx_dict())
    assert not r.ok and "GUARD_SNAPSHOT_SOURCE_AMBIGUOUS" in r.reasons


def test_guard_flat_source_unknown_ambiguous(tmp_path):
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, {"source": "backfill", "positions": 0})
    r = guard_flat_no_pending(str(pf), _live_ctx_dict())
    assert not r.ok and "GUARD_SNAPSHOT_SOURCE_AMBIGUOUS" in r.reasons


def test_guard_flat_stale_captured_at_refused(tmp_path):
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, _live_snapshot(captured_at=time.time() - 3600))
    r = guard_flat_no_pending(str(pf), _live_ctx_dict())
    assert not r.ok and "GUARD_SNAPSHOT_STALE" in r.reasons


def test_guard_flat_missing_hash_refused(tmp_path):
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, _live_snapshot(hash=None))
    r = guard_flat_no_pending(str(pf), _live_ctx_dict())
    assert not r.ok and "GUARD_SNAPSHOT_HASH_MISSING" in r.reasons


def test_guard_flat_intervening_live_session_refused(tmp_path):
    # a live session that started after the snapshot = intervening session
    from core.deployment_safety_gate import guard_flat_no_pending
    rt = tmp_path / "rt"
    pf = _position_file(rt, _live_snapshot(session_id="sess-OLD"))
    r = guard_flat_no_pending(str(pf), _live_ctx_dict())  # ctx sess-1
    assert not r.ok and "GUARD_SESSION_INTERVENED" in r.reasons


# ── 6. quarantine-first startup (AST) ──────────────────────────────────────

def test_guard_quarantine_first_startup(tmp_path):
    from core.deployment_safety_gate import guard_quarantine_first_startup
    repo = _git_repo(tmp_path)
    mon = repo / "monitor.py"
    mon.write_text(_monitor_text_gated(), encoding="utf-8")
    r = guard_quarantine_first_startup(str(mon))
    assert r.ok, r.reasons


def test_guard_quarantine_first_startup_unsafe(tmp_path):
    from core.deployment_safety_gate import guard_quarantine_first_startup
    repo = _git_repo(tmp_path)
    mon = repo / "monitor.py"
    mon.write_text(
        "def __init__(self):\n"
        "    if self.live_trading:\n"
        "        self._execution_context = None\n"
        "        self.api.place_order(None)\n"
        "def _place_safety_stop(self):\n"
        "    self.api.place_order(None)\n",
        encoding="utf-8")
    r = guard_quarantine_first_startup(str(mon))
    assert not r.ok and "GUARD_STARTUP_UNSAFE" in r.reasons


# ── 7. session/cert generation ─────────────────────────────────────────────

def test_guard_session_generation_ok():
    from core.deployment_safety_gate import guard_session_generation
    assert guard_session_generation(42, revoked=False).ok


def test_guard_session_generation_missing():
    from core.deployment_safety_gate import guard_session_generation
    r = guard_session_generation(None, revoked=False)
    assert not r.ok and "GUARD_SESSION_MISSING" in r.reasons


def test_guard_session_generation_revoked():
    from core.deployment_safety_gate import guard_session_generation
    r = guard_session_generation(42, revoked=True)
    assert not r.ok and "GUARD_SESSION_REVOKED" in r.reasons


# ── 8. margin ──────────────────────────────────────────────────────────────

def test_guard_margin_ok():
    from core.deployment_safety_gate import guard_margin
    assert guard_margin(300_000.0).ok
    assert guard_margin(220_000.0).ok          # exact boundary


def test_guard_margin_below():
    from core.deployment_safety_gate import guard_margin
    r = guard_margin(219_999.99)
    assert not r.ok and "GUARD_MARGIN_INSUFFICIENT" in r.reasons


def test_guard_margin_none_and_invalid():
    from core.deployment_safety_gate import guard_margin
    assert "GUARD_MARGIN_UNAVAILABLE" in guard_margin(None).reasons
    assert "GUARD_MARGIN_INVALID" in guard_margin(float("nan")).reasons
    assert "GUARD_MARGIN_INVALID" in guard_margin(-1.0).reasons


# ── 9. rollback manifest + drift abort ─────────────────────────────────────

def test_guard_rollback_manifest_ok(tmp_path):
    from core.deployment_safety_gate import guard_rollback_manifest
    m = tmp_path / "PHASE1_RC_CANDIDATE.md"
    m.write_text(f"Frozen SHA {SHA}", encoding="utf-8")
    r = guard_rollback_manifest([str(m)], SHA)
    assert r.ok, r.reasons


def test_guard_rollback_manifest_stale(tmp_path):
    from core.deployment_safety_gate import guard_rollback_manifest
    m = tmp_path / "PHASE1_RC_CANDIDATE.md"
    m.write_text(f"Frozen SHA {OTHER}", encoding="utf-8")
    r = guard_rollback_manifest([str(m)], SHA)
    assert not r.ok and "GUARD_MANIFEST_STALE" in r.reasons


def test_guard_rollback_manifest_missing(tmp_path):
    from core.deployment_safety_gate import guard_rollback_manifest
    r = guard_rollback_manifest([str(tmp_path / "nope.md")], SHA)
    assert not r.ok and "GUARD_MANIFEST_MISSING" in r.reasons


# ── 10. ctx atomic read/write health ───────────────────────────────────────

def test_guard_ctx_atomic_health_ok(tmp_path):
    from core.deployment_safety_gate import guard_ctx_atomic_health
    rt = tmp_path / "rt"
    _ctx_file(rt, _ready_ctx_dict())
    r = guard_ctx_atomic_health(str(rt))
    assert r.ok, r.reasons


def test_guard_ctx_atomic_health_corrupt(tmp_path):
    from core.deployment_safety_gate import guard_ctx_atomic_health
    rt = tmp_path / "rt"
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "execution_context.json").write_text("{not json", encoding="utf-8")
    r = guard_ctx_atomic_health(str(rt))
    assert not r.ok and "GUARD_CTX_CORRUPT" in r.reasons


# ── aggregate fail-closed decision ─────────────────────────────────────────

def test_aggregate_all_pass_readies(tmp_path, monkeypatch):
    from core.deployment_safety_gate import check_deployment
    repo = _git_repo(tmp_path)
    head = _head(repo)
    rt = tmp_path / "rt"
    _ctx_file(rt, _live_ctx_dict())
    pf = _position_file(rt, _live_snapshot())
    manifest = tmp_path / "PHASE1_RC_CANDIDATE.md"
    manifest.write_text(f"Frozen SHA {head}", encoding="utf-8")
    monkeypatch.setenv("LRC_RELEASE_SHA", head)
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(rt))
    c = check_deployment(
        release_dir=str(repo), closure_files=CLOSURE,
        runtime_dir=str(rt), pid_file=str(tmp_path / "nope.pid"),
        position_state_path=str(pf), monitor_path=None,
        session_generation=1, margin_available=300_000.0,
        manifest_paths=[str(manifest)], expected_sha=head)
    assert c.ok, [(g.guard, g.reasons) for g in c.results]
    assert c.refusal_codes == ()


def test_aggregate_any_fail_blocks(tmp_path, monkeypatch):
    # one failing guard -> NOT_READY with the refusal code surfaced
    from core.deployment_safety_gate import check_deployment
    repo = _git_repo(tmp_path)
    head = _head(repo)
    rt = tmp_path / "rt"
    _ctx_file(rt, _live_ctx_dict())
    pf = _position_file(rt, _live_snapshot(positions=2))
    monkeypatch.setenv("LRC_RELEASE_SHA", head)
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(rt))
    c = check_deployment(
        release_dir=str(repo), closure_files=CLOSURE,
        runtime_dir=str(rt), pid_file=str(tmp_path / "nope.pid"),
        position_state_path=str(pf), monitor_path=None,
        session_generation=1, margin_available=300_000.0,
        manifest_paths=[], expected_sha=head)
    assert not c.ok
    assert "GUARD_POSITION_NOT_FLAT" in c.refusal_codes
    assert "GUARD_MANIFEST_MISSING" in c.refusal_codes
    assert c.refusal_codes == tuple(sorted(c.refusal_codes))
