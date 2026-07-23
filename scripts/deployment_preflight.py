#!/usr/bin/env python3
"""
Preflight checker for Air4–Mini deployment identity.
Validates .deployment-target, repo state, and host consistency.

Usage:
    python3 scripts/deployment_preflight.py

Returns exit code 0 if all checks pass, non-zero otherwise.
Failed checks print DEPLOYMENT_<ERROR> tokens for automated parsing.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT_FILE = REPO_ROOT / ".deployment-target"

ERRORS: list[str] = []
WARNINGS: list[str] = []


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def check_deployment_file():
    if not DEPLOYMENT_FILE.exists():
        ERRORS.append("DEPLOYMENT_IDENTITY_MISSING")
        ERRORS.append(f"  expected: {DEPLOYMENT_FILE}")
        return None

    try:
        with open(DEPLOYMENT_FILE) as f:
            identity = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        ERRORS.append(f"DEPLOYMENT_IDENTITY_INVALID: {e}")
        return None

    # Schema version
    sv = identity.get("schema_version")
    if sv != 1:
        ERRORS.append(f"DEPLOYMENT_SCHEMA_VERSION_UNSUPPORTED: {sv}")

    # Required fields
    for field in ("deployment_id", "host_role", "instance_id", "repo_path", "allowed_operations"):
        if field not in identity:
            ERRORS.append(f"DEPLOYMENT_IDENTITY_MISSING_FIELD: {field}")

    # Repo path match
    resolved_identity_path = Path(identity.get("repo_path", "")).resolve()
    resolved_actual_path = REPO_ROOT.resolve()
    if resolved_identity_path != resolved_actual_path:
        ERRORS.append("DEPLOYMENT_REPO_PATH_MISMATCH")
        ERRORS.append(f"  identity: {resolved_identity_path}")
        ERRORS.append(f"  actual:   {resolved_actual_path}")

    return identity


def check_git_state():
    # Repo root
    git_root = _run(["git", "rev-parse", "--show-toplevel"])
    if Path(git_root).resolve() != REPO_ROOT.resolve():
        ERRORS.append(f"GIT_ROOT_MISMATCH: git={git_root} vs resolved={REPO_ROOT}")

    # Commit
    commit = _run(["git", "rev-parse", "--short", "HEAD"])
    WARNINGS.append(f"  git_commit: {commit}")

    # Dirty
    dirty = _run(["git", "status", "--short"])
    if dirty:
        WARNINGS.append(f"  git_dirty: True ({len(dirty.split(chr(10)))} files)")


def check_host():
    hostname = _run(["hostname"])
    computer = _run(["scutil", "--get", "ComputerName"]) if sys.platform == "darwin" else "N/A"
    WARNINGS.append(f"  hostname: {hostname}")
    WARNINGS.append(f"  computer_name: {computer}")


def main():
    identity = check_deployment_file()
    check_git_state()
    check_host()

    print(f"=== Deployment Preflight ===")
    print(f"deployment_id: {identity.get('deployment_id', '?') if identity else 'MISSING'}")
    print(f"host_role: {identity.get('host_role', '?') if identity else '?'}")
    print(f"instance_id: {identity.get('instance_id', '?') if identity else '?'}")
    print(f"repo_root: {REPO_ROOT}")
    for w in WARNINGS:
        print(w)

    if ERRORS:
        print("\n--- ERRORS ---")
        for e in ERRORS:
            print(e)
        sys.exit(1)
    else:
        print("\nALL CHECKS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
