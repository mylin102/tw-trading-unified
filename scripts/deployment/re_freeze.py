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
import subprocess
import sys
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
)
UNTRACKED_ALLOWED_FILES = (".DS_Store",)


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release-dir", default=str(Path.cwd()))
    ap.add_argument("--expected-sha", default=None)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--exclude-path", action="append", default=[])
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
            print(f"VERIFY_ONLY OK HEAD={head} frozen_tree_hash={recorded}")
            print("VERIFY_ONLY: this is NOT a deploy-ready gate — run "
                  "check_deployment.py for the real pre_deploy decision "
                  "(clean tracked+untracked policy applies there)")
            return 0
        print(f"VERIFY_ONLY FAIL recorded={recorded} != HEAD tree={cur} "
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
    manifest.write_text(updated, encoding="utf-8")
    print(f"re-freeze recorded: HEAD={head}")
    print(f"frozen_tree_hash: {tree_hash}")
    print(f"manifest: {manifest}")
    print("NOTE: commit this manifest update as the freeze record; "
          "the exclude-self identity makes manifest-only commits "
          "non-moving (no freeze/manifest SHA cycle).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
