"""Release identity verifier (phase2 §8.5 / §9.4 — release-dir scoped).

The LIVE startup path verifies that the ACTUAL release tree's HEAD equals
the injected LRC_RELEASE_SHA BEFORE any certification/transition. The
check runs `git -C <release_dir> rev-parse HEAD` — never an arbitrary
cwd. Fail-closed on every failure mode:

- env missing / not a 40-hex SHA -> RELEASE_IDENTITY_ENV_MISSING
- git command failure (non-zero, non-hex output) -> RELEASE_IDENTITY_GIT_FAILED
- HEAD != expected -> RELEASE_IDENTITY_MISMATCH

The runner is injectable for tests; the default uses subprocess with
`git -C <release_dir>` (release-dir scoped, cwd-independent).
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, List, Optional, Tuple

Runner = Callable[[List[str], Optional[str]], Tuple[int, str, str]]


def _is_hex40(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdefABCDEF"
                                    for c in value)


def _default_runner(cmd: List[str], cwd: Optional[str] = None):
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                          timeout=30)
    return proc.returncode, proc.stdout, proc.stderr


def verify_release_identity(release_dir: str,
                            runner: Optional[Runner] = None,
                            expected: Optional[str] = None) -> Tuple[bool, List[str]]:
    """Verify release-tree HEAD == LRC_RELEASE_SHA.

    Returns (ok, reasons). Never raises on git/env failures — always a
    structured fail-closed outcome.
    """
    reasons: List[str] = []
    expected_sha = expected if expected is not None \
        else os.environ.get("LRC_RELEASE_SHA", "")
    if not expected_sha or not _is_hex40(expected_sha):
        return False, ["RELEASE_IDENTITY_ENV_MISSING"]
    if runner is None:
        runner = _default_runner
    try:
        code, out, err = runner(
            ["git", "-C", str(release_dir), "rev-parse", "HEAD"])
    except Exception:
        return False, ["RELEASE_IDENTITY_GIT_FAILED"]
    if code != 0:
        return False, ["RELEASE_IDENTITY_GIT_FAILED"]
    head = (out or "").strip()
    if not _is_hex40(head):
        return False, ["RELEASE_IDENTITY_GIT_FAILED"]
    if head != expected_sha:
        return False, ["RELEASE_IDENTITY_MISMATCH"]
    return True, []
