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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release-dir", default=str(Path.cwd()))
    ap.add_argument("--expected-sha", default=None)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--exclude-path", action="append", default=[])
    ap.add_argument("--ignore-untracked", action="append", default=[],
                    help="glob pattern (fnmatch) of untracked runtime "
                         "artifacts that are explicitly controlled and "
                         "allowed to remain; everything else untracked "
                         "blocks the re-freeze")
    args = ap.parse_args()

    repo = Path(args.release_dir).resolve()
    if not (repo / ".git").exists():
        print(f"FATAL not a git repo: {repo}", file=sys.stderr)
        return 1

    # fail-closed #1: head drift
    head = _run(repo, "rev-parse", "HEAD")
    if args.expected_sha and args.expected_sha != head:
        print(f"ABORT head drift: expected {args.expected_sha} "
              f"!= HEAD {head}", file=sys.stderr)
        return 2

    # fail-closed #2: dirty tree — TRACKED modifications AND untracked
    # files both block (freeze-last: nothing may change after the freeze;
    # untracked runtime artifacts only pass with an explicit controlled
    # --ignore-untracked pattern). The re-record target manifest itself is
    # excluded (its update IS the script's job).
    exclude = args.exclude_path or DEFAULT_EXCLUDE
    manifest = Path(args.manifest).resolve() if args.manifest \
        else repo / "PHASE1_FINAL_FREEZE.md"
    status = _run(repo, "status", "--porcelain", "-uall")
    _manifest_rel = manifest.name
    tracked_dirty = [ln for ln in status.splitlines()
                     if not ln.startswith("??")
                     and not ln.strip().endswith(_manifest_rel)]
    untracked = [ln[3:] for ln in status.splitlines()
                 if ln.startswith("??")]
    if tracked_dirty:
        print(f"ABORT dirty tree ({len(tracked_dirty)} changed "
              f"file(s)) — finish all commits before re-freezing:\n"
              + "\n".join(tracked_dirty[:10]), file=sys.stderr)
        return 3
    import fnmatch
    remaining = []
    for u in untracked:
        if any(fnmatch.fnmatch(u, p) for p in args.ignore_untracked):
            continue
        remaining.append(u)
    if remaining:
        print(f"ABORT untracked files ({len(remaining)}) block the "
              f"re-freeze — commit them or pass --ignore-untracked "
              f"patterns for controlled runtime artifacts:\n"
              + "\n".join(remaining[:10]), file=sys.stderr)
        return 6

    if not manifest.is_file():
        print(f"FATAL manifest not found: {manifest}", file=sys.stderr)
        return 4

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
