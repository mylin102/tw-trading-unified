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

    # fail-closed #2: dirty closure tree (nothing may change after the
    # freeze — the re-record is manifest-only)
    status = _run(repo, "status", "--porcelain")
    dirty = [ln for ln in status.splitlines()
             if not ln.startswith("??")]
    if dirty:
        print(f"ABORT dirty tree ({len(dirty)} changed file(s)) — "
              f"finish all commits before re-freezing:\n"
              + "\n".join(dirty[:10]), file=sys.stderr)
        return 3

    exclude = args.exclude_path or DEFAULT_EXCLUDE
    manifest = Path(args.manifest).resolve() if args.manifest \
        else repo / "PHASE1_FINAL_FREEZE.md"
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
