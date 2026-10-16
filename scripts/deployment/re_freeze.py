#!/usr/bin/env python3
"""re_freeze.py — controlled freeze-record re-record (Codex decision D4).

Recomputes the exclude-self tree identity (``git ls-tree -r HEAD`` minus
the manifest/rollback docs) at the CURRENT HEAD and updates the freeze
record's ``frozen_tree_hash``. Fail-closed:

  * refuses when the closure tree is dirty (uncommitted changes) —
    freeze-last, never edit after freezing;
  * refuses when ``--expected-sha`` is given and ``rev-parse HEAD``
    differs (head drift => ABORT).

Usage:
  re_freeze.py [--release-dir DIR] [--expected-sha SHA]
               [--manifest PATH] [--exclude-path PATH ...]

Never deploys / restarts / unlocks LIVE — this only updates the rollback
manifest so the deployment gate's GUARD_MANIFEST_STALE resolves honestly.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_EXCLUDE = [
    "PHASE1_RC_CANDIDATE.md",
    "PHASE2_DEPLOYMENT_MANIFEST.md",
    "PHASE1_FINAL_FREEZE.md",
]

# Program-fixed allowlist of untracked RUNTIME artifacts that may remain
# during a re-freeze. NO operator-supplied globs are accepted — anything
# outside these prefixes (core/**, strategies/**, config/**, scripts/**,
# tests/**, new top-level files, ...) blocks the re-freeze.
UNTRACKED_ALLOWED_PREFIXES = (
    "data/telemetry/",
    "data/research/",
    "data/backtest/",
    "logs/",
    ".venv/",
    "commands/",
)
UNTRACKED_ALLOWED_FILES = (
    ".DS_Store",
    "execution_context.json",
    "exit_only_renewal_provenance.json",
)


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        check=True).stdout.strip()


def _exclude_self_tree_hash(repo: Path, commit: str,
                            exclude: list) -> str:
    out = _run(repo, "ls-tree", "-r", commit)
    lines = [ln for ln in out.splitlines()
             if not any(ln.split("\t")[-1] == p for p in exclude)]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _normalized_untracked(repo: Path, raw: str):
    """Normalize an untracked path from git status; None => escape/reject.
    Rejects `..`, absolute paths and symlink escapes (resolved path must
    stay inside the repo)."""
    p = Path(raw)
    if p.is_absolute() or ".." in p.parts:
        return None
    try:
        resolved = (repo / p).resolve()
        root = repo.resolve()
        if resolved != root and root not in resolved.parents:
            return None                    # symlink/.. escape
    except OSError:
        return None
    return p.as_posix()


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace *path* atomically and durably in its containing directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.",
                                    dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _runtime_metadata(repo: Path, runtime_dir: Path, tree_hash: str, head: str,
                      pid: int | None, callback_generation: int | None,
                      config_profile: Path | None, ttl_seconds: float) -> dict:
    """Build non-secret runtime binding metadata for the freeze record."""
    ctx = {}
    canonical = {}
    locks = {}
    ctx_path = runtime_dir / "execution_context.json"
    canonical_path = runtime_dir / "exports/trades/live/diagnostics/broker_snapshot_canonical.json"
    locks_path = runtime_dir / "exports/trades/live/diagnostics/mts_leg_locks.json"
    for path, target in ((ctx_path, "ctx"), (canonical_path, "canonical"),
                         (locks_path, "locks")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if target == "ctx": ctx = value
            elif target == "canonical": canonical = value
            else: locks = value
        except (OSError, ValueError):
            pass
    rows = canonical.get("positions") or []
    futures_rows = [r for r in rows if r.get("account") == "futures"]
    lock_rows = locks.get("locks", locks) if isinstance(locks, dict) else locks
    if isinstance(lock_rows, dict):
        lock_rows = list(lock_rows.values())
    lock_rows = lock_rows or []
    retired = sum(1 for r in lock_rows
                  if r.get("status") == "RETIRED_UNRESOLVED")
    active = sum(1 for r in lock_rows
                 if r.get("status") not in {"RETIRED_UNRESOLVED", "RETIRED"})
    process_identity = None
    if pid is not None:
        try:
            proc = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                  capture_output=True, text=True, timeout=5)
            process_identity = proc.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            process_identity = None
    config_hash = None
    if config_profile is not None:
        try:
            config_hash = hashlib.sha256(config_profile.read_bytes()).hexdigest()
        except OSError:
            config_hash = None
    return {
        "candidate_head": head,
        "clean_worktree": not bool(_run(repo, "status", "--porcelain", "-uall")),
        "source_tree_hash": tree_hash,
        "process": {"pid": pid, "start_identity": process_identity},
        "session_id": canonical.get("session_id") or ctx.get("session_id"),
        "refresh_generation": canonical.get("snapshot_generation"),
        "captured_at": canonical.get("captured_at"),
        "broker": {
            "source": canonical.get("source"),
            "mode": canonical.get("mode"),
            "capture": (canonical.get("fetch_status") or {}).get("capture"),
            "futures_positions": len(futures_rows),
            "active_orders": len(canonical.get("open_orders") or []),
        },
        "durable_locks": {"active": active, "retired_unresolved": retired},
        "callback_registration_generation": callback_generation,
        "config_hash": config_hash,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": ttl_seconds,
        "promotion_mode": ctx.get("effective_mode"),
        "live_order_allowed": ctx.get("live_order_allowed"),
    }


def _embed_runtime_metadata(body: str, metadata: dict) -> str:
    start = "\n## Runtime certification metadata\n<!-- BEGIN RUNTIME_CERTIFICATION_METADATA -->\n"
    end = "<!-- END RUNTIME_CERTIFICATION_METADATA -->\n"
    block = start + "```json\n" + json.dumps(metadata, indent=2,
                                                  sort_keys=True) + "\n```\n" + end
    begin_marker = "<!-- BEGIN RUNTIME_CERTIFICATION_METADATA -->"
    end_marker = "<!-- END RUNTIME_CERTIFICATION_METADATA -->"
    if begin_marker in body and end_marker in body:
        left = body[:body.index(begin_marker)]
        right = body[body.index(end_marker) + len(end_marker):]
        return left.rstrip() + "\n" + block.lstrip("\n") + right
    return body.rstrip() + "\n" + block


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release-dir", default=str(Path.cwd()))
    ap.add_argument("--expected-sha", default=None)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--exclude-path", action="append", default=[])
    ap.add_argument("--runtime-dir", default=None,
                    help="runtime root to bind into certification metadata")
    ap.add_argument("--pid", type=int, default=None,
                    help="running process PID to bind into metadata")
    ap.add_argument("--callback-registration-generation", type=int,
                    default=None)
    ap.add_argument("--config-profile", default=None)
    ap.add_argument("--ttl-seconds", type=float, default=600.0)
    ap.add_argument("--backup-dir", default=None,
                    help="out-of-tree directory for the prior manifest + SHA256")
    ap.add_argument("--verify", action="store_true",
                    help="read-only: verify the recorded frozen_tree_hash "
                         "matches the CURRENT HEAD's exclude-self tree "
                         "identity (no writes)")
    args = ap.parse_args()

    repo = Path(args.release_dir).resolve()
    if not (repo / ".git").exists():
        print(f"FATAL not a git repo: {repo}", file=sys.stderr)
        return 1

    exclude = args.exclude_path or DEFAULT_EXCLUDE
    manifest = Path(args.manifest).resolve() if args.manifest \
        else repo / "PHASE1_FINAL_FREEZE.md"

    # fail-closed #1: head drift (record mode)
    head = _run(repo, "rev-parse", "HEAD")
    if not args.verify and args.expected_sha and args.expected_sha != head:
        print(f"ABORT head drift: expected {args.expected_sha} "
              f"!= HEAD {head}", file=sys.stderr)
        return 2

    # fail-closed #2: dirty tree — TRACKED modifications (INCLUDING a
    # hand-staged manifest) AND untracked files block. Freeze-first: the
    # index/worktree must be clean before the re-record; the re-record
    # target manifest must be committed (a staged manifest is an
    # operator bypass — rejected). Untracked runtime artifacts pass only
    # the program-fixed allowlist (no operator globs); paths are
    # normalized first — `..`, absolute paths and symlink escapes are
    # rejected outright. (--verify is read-only and skips these.)
    if not args.verify:
        status = _run(repo, "status", "--porcelain", "-uall")
        tracked_dirty = [ln for ln in status.splitlines()
                         if not ln.startswith("??")]
        untracked = [ln[3:] for ln in status.splitlines()
                     if ln.startswith("??")]
        if tracked_dirty:
            print(f"ABORT dirty tree ({len(tracked_dirty)} changed "
                  f"file(s)) — index/worktree must be clean (the manifest "
                  f"included) before re-freezing:\n"
                  + "\n".join(tracked_dirty[:10]), file=sys.stderr)
            return 3
        remaining = []
        for u in untracked:
            norm = _normalized_untracked(repo, u)
            if norm is None:
                remaining.append(f"{u} (path escape)")
                continue
            if any(norm.startswith(p) or norm == p.rstrip("/")
                   for p in UNTRACKED_ALLOWED_PREFIXES) or \
                    norm in UNTRACKED_ALLOWED_FILES:
                continue
            remaining.append(u)
        if remaining:
            print(f"ABORT untracked files ({len(remaining)}) block the "
                  f"re-freeze — only the program-fixed runtime allowlist "
                  f"may remain (commit everything else):\n"
                  + "\n".join(remaining[:10]), file=sys.stderr)
            return 6

    if not manifest.is_file():
        print(f"FATAL manifest not found: {manifest}", file=sys.stderr)
        return 4
    body = manifest.read_text(encoding="utf-8")
    import re
    m = re.search(r"^frozen_tree_hash:\s*([0-9a-f]{64})$", body, re.M)
    if not m:
        print(f"FATAL {manifest.name} lacks a frozen_tree_hash line",
              file=sys.stderr)
        return 5
    recorded = m.group(1)
    if args.verify:
        cur = _exclude_self_tree_hash(repo, "HEAD", exclude)
        if cur == recorded:
            print(f"phase=VERIFY_ONLY OK HEAD={head} "
                  f"frozen_tree_hash={recorded}")
            print("VERIFY_ONLY: this is NOT a deploy-ready gate — run "
                  "check_deployment.py for the real pre_deploy decision "
                  "(clean tracked+untracked policy applies there)")
            return 0
        print(f"phase=VERIFY_ONLY FAIL recorded={recorded} != HEAD tree={cur} "
              f"(stale or tree moved)", file=sys.stderr)
        return 7

    tree_hash = _exclude_self_tree_hash(repo, head, exclude)
    body = manifest.read_text(encoding="utf-8")
    import re
    if not re.search(r"^frozen_tree_hash:\s*[0-9a-f]{64}$", body, re.M):
        print(f"FATAL {manifest.name} lacks a frozen_tree_hash line",
              file=sys.stderr)
        return 5
    updated = re.sub(
        r"(?m)^frozen_tree_hash:\s*[0-9a-f]{64}$",
        f"frozen_tree_hash: {tree_hash}", body)
    if args.runtime_dir:
        updated = _embed_runtime_metadata(
            updated,
            _runtime_metadata(
                repo, Path(args.runtime_dir).resolve(), tree_hash, head, args.pid,
                args.callback_registration_generation,
                Path(args.config_profile).resolve()
                if args.config_profile else None,
                args.ttl_seconds))
    if args.backup_dir:
        backup_dir = Path(args.backup_dir).resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_dir / f"{manifest.name}.{stamp}.bak"
        prior = manifest.read_bytes()
        _atomic_write_text(backup, prior.decode("utf-8"))
        digest = hashlib.sha256(prior).hexdigest()
        _atomic_write_text(backup.with_suffix(backup.suffix + ".sha256"),
                           digest + "  " + backup.name + "\n")
        print(f"manifest_backup: {backup}")
        print(f"manifest_backup_sha256: {digest}")
    _atomic_write_text(manifest, updated)
    print(f"re-freeze recorded: HEAD={head}")
    print(f"frozen_tree_hash: {tree_hash}")
    print(f"manifest: {manifest}")
    print("NOTE: commit this manifest update as the freeze record; "
          "the exclude-self identity makes manifest-only commits "
          "non-moving (no freeze/manifest SHA cycle).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
