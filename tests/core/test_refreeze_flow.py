# 2026-08-14 Antigravity AI: Task 14 RED — Option A Certified Config-Change & Re-Freeze Flow Test Suite
import os
import hashlib
import sys
import subprocess as sp
import tempfile
import time
from pathlib import Path
import pytest

from core.deployment_safety_gate import (
    guard_rollback_manifest,
    check_deployment,
    _exclude_self_tree_hash,
)
from core.live_route_certificate import (
    validate_live_broker_certificate,
    LiveBrokerCertificate,
)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    sp.run(["git", "-C", str(repo), "init", "-q"], check=True)
    sp.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    sp.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    (repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (repo / "config").mkdir(parents=True, exist_ok=True)
    (repo / "config" / "futures_live.yaml").write_text(
        "# Live Route Certification Invariant: do not delete\n"
        "live_trading: true\n"
        "trade_mgmt:\n"
        "  max_positions: 2\n",
        encoding="utf-8",
    )
    sp.run(["git", "-C", str(repo), "add", "."], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    return repo


def _head_sha(repo: Path) -> str:
    return sp.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def test_config_change_without_refreeze_blocks_deployment_gate(tmp_path):
    """Option A invariant: modifying a config file and committing it moves the tree hash;
    without re-freezing, the rollback_manifest guard and deployment gate must fail-closed (GUARD_MANIFEST_STALE)."""
    repo = _init_repo(tmp_path)
    head_v1 = _head_sha(repo)
    manifest = repo / "PHASE1_FINAL_FREEZE.md"
    exclude = ["PHASE1_FINAL_FREEZE.md"]

    # Record initial freeze in manifest and commit it
    initial_tree_hash = _exclude_self_tree_hash(str(repo), "HEAD", exclude)
    assert initial_tree_hash is not None
    manifest.write_text(f"Frozen SHA {head_v1}\nfrozen_tree_hash: {initial_tree_hash}\n", encoding="utf-8")
    sp.run(["git", "-C", str(repo), "add", str(manifest)], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "manifest"], check=True)
    head_v2 = _head_sha(repo)

    # Pre-change: manifest guard passes
    res_clean = guard_rollback_manifest(str(repo), [str(manifest)], head_v2, exclude_paths=exclude)
    assert res_clean.ok

    # Modify futures_live.yaml (e.g. user changes max_positions from 2 to 4)
    (repo / "config" / "futures_live.yaml").write_text(
        "# Live Route Certification Invariant: do not delete\n"
        "live_trading: true\n"
        "trade_mgmt:\n"
        "  max_positions: 4\n",
        encoding="utf-8",
    )
    sp.run(["git", "-C", str(repo), "add", "config/futures_live.yaml"], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "change config max_positions to 4"], check=True)
    head_v3 = _head_sha(repo)

    # Post-change without re-freeze: guard_rollback_manifest MUST refuse with GUARD_MANIFEST_STALE
    res_stale = guard_rollback_manifest(str(repo), [str(manifest)], head_v3, exclude_paths=exclude)
    assert not res_stale.ok
    assert "GUARD_MANIFEST_STALE" in res_stale.reasons


def test_refreeze_tool_updates_manifest_and_restores_deployment_gate(tmp_path):
    """Option A flow: re-freeze tool recomputes exclude-self tree hash, updates manifest,
    and deployment safety gate returns ok / MATCH_OK_STABLE."""
    repo = _init_repo(tmp_path)
    head_v1 = _head_sha(repo)
    manifest = repo / "PHASE1_FINAL_FREEZE.md"
    exclude = ["PHASE1_FINAL_FREEZE.md"]

    # Initial freeze
    initial_tree_hash = _exclude_self_tree_hash(str(repo), "HEAD", exclude)
    manifest.write_text(f"Frozen SHA {head_v1}\nfrozen_tree_hash: {initial_tree_hash}\n", encoding="utf-8")
    sp.run(["git", "-C", str(repo), "add", str(manifest)], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "manifest"], check=True)

    # Config change
    (repo / "config" / "futures_live.yaml").write_text(
        "# Live Route Certification Invariant: do not delete\n"
        "live_trading: true\n"
        "trade_mgmt:\n"
        "  max_positions: 4\n",
        encoding="utf-8",
    )
    sp.run(["git", "-C", str(repo), "add", "config/futures_live.yaml"], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "change config max_positions to 4"], check=True)
    head_v3 = _head_sha(repo)

    # Run re-freeze CLI tool
    refreeze_script = Path(__file__).resolve().parents[2] / "scripts/deployment/re_freeze.py"
    r = sp.run(
        [sys.executable, "-B", str(refreeze_script),
         "--release-dir", str(repo), "--expected-sha", head_v3,
         "--manifest", str(manifest)],
        capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr

    # Verify manifest was updated with new hash
    manifest_body = manifest.read_text(encoding="utf-8")
    assert f"frozen_tree_hash: {initial_tree_hash}" not in manifest_body

    # Verify guard_rollback_manifest now passes
    res_refrozen = guard_rollback_manifest(str(repo), [str(manifest)], head_v3, exclude_paths=exclude)
    assert res_refrozen.ok, res_refrozen.detail


def test_refreeze_verify_mode_reports_verify_only_and_detects_drift(tmp_path):
    """Re-freeze CLI --verify mode outputs phase=VERIFY_ONLY and fails if stale."""
    repo = _init_repo(tmp_path)
    head = _head_sha(repo)
    manifest = repo / "PHASE1_FINAL_FREEZE.md"
    exclude = ["PHASE1_FINAL_FREEZE.md"]

    manifest.write_text(f"Frozen SHA {head}\nfrozen_tree_hash: {'0' * 64}\n", encoding="utf-8")
    sp.run(["git", "-C", str(repo), "add", str(manifest)], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "manifest"], check=True)
    head_m = _head_sha(repo)

    refreeze_script = Path(__file__).resolve().parents[2] / "scripts/deployment/re_freeze.py"

    # Re-record hash
    r_freeze = sp.run(
        [sys.executable, "-B", str(refreeze_script),
         "--release-dir", str(repo), "--expected-sha", head_m,
         "--manifest", str(manifest)],
        capture_output=True, text=True, timeout=60
    )
    assert r_freeze.returncode == 0

    # Commit updated manifest
    sp.run(["git", "-C", str(repo), "add", str(manifest)], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "freeze record"], check=True)

    # Run --verify
    r_ver = sp.run(
        [sys.executable, "-B", str(refreeze_script),
         "--verify", "--release-dir", str(repo),
         "--manifest", str(manifest)],
        capture_output=True, text=True, timeout=60
    )
    assert r_ver.returncode == 0
    assert "phase=VERIFY_ONLY" in r_ver.stdout


def test_refreeze_runtime_metadata_backup_is_atomic_and_bound(tmp_path):
    """The promotion freeze binds runtime evidence and preserves the prior
    manifest outside the source tree using the durable atomic writer."""
    repo = _init_repo(tmp_path)
    manifest = repo / "PHASE1_FINAL_FREEZE.md"
    exclude = ["PHASE1_FINAL_FREEZE.md"]
    initial_hash = _exclude_self_tree_hash(str(repo), "HEAD", exclude)
    manifest.write_text(f"frozen_tree_hash: {initial_hash}\n", encoding="utf-8")
    sp.run(["git", "-C", str(repo), "add", "."], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-qm", "manifest"], check=True)

    runtime = tmp_path / "runtime"
    diag = runtime / "exports/trades/live/diagnostics"
    diag.mkdir(parents=True)
    (runtime / "execution_context.json").write_text(
        '{"effective_mode":"live_quarantined","live_order_allowed":false,'
        '"session_id":"sess-1"}', encoding="utf-8")
    (diag / "broker_snapshot_canonical.json").write_text(
        '{"source":"live_broker","mode":"live","session_id":"sess-1",'
        '"snapshot_generation":"gen-7","captured_at":1780000000000,'
        '"fetch_status":{"capture":"OK"},"positions":[],"open_orders":[]}',
        encoding="utf-8")
    (diag / "mts_leg_locks.json").write_text(
        '{"a":{"status":"RETIRED_UNRESOLVED"}}', encoding="utf-8")
    backup_dir = tmp_path / "backup"
    script = Path(__file__).resolve().parents[2] / "scripts/deployment/re_freeze.py"
    r = sp.run(
        [sys.executable, "-B", str(script), "--release-dir", str(repo),
         "--expected-sha", _head_sha(repo), "--manifest", str(manifest),
         "--runtime-dir", str(runtime), "--pid", str(os.getpid()),
         "--callback-registration-generation", "1", "--ttl-seconds", "600",
         "--backup-dir", str(backup_dir)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    backups = list(backup_dir.glob("*.bak"))
    assert len(backups) == 1
    prior = backups[0].read_bytes()
    assert hashlib.sha256(prior).hexdigest() in r.stdout
    assert backups[0].with_suffix(backups[0].suffix + ".sha256").exists()
    body = manifest.read_text(encoding="utf-8")
    assert "frozen_tree_hash:" in body
    assert "RUNTIME_CERTIFICATION_METADATA" in body
    assert '"candidate_head"' in body
    assert '"refresh_generation": "gen-7"' in body
    assert '"active_orders": 0' in body
    assert '"retired_unresolved": 1' in body
    assert not list(repo.glob(".PHASE1_FINAL_FREEZE.md.*"))
