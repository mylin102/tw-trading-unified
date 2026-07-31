#!/usr/bin/env python3
"""Runtime Manifest Gate — verify the deployed runtime matches the expected repo.

Checks (in order):
  1. git top-level is the authoritative repo (tw-trading-unified-git)
  2. HEAD matches --expected-sha when provided (deployment manifest SHA)
  3. working tree is clean (unless --allow-dirty; --strict-dirty fails on ANY change)
  4. pm2 apps (trading-system, dashboard) exec path & cwd live under the repo

Exit codes:
  0 = pass
  1 = wrong repo / wrong SHA / dirty tree (strict) / wrong PM2 cwd / error

Usage:
  python3 scripts/ci/check_runtime_manifest.py \
      --expected-repo tw-trading-unified-git \
      [--expected-sha "$DEPLOY_SHA"] \
      [--strict-dirty] \
      [--apps trading-system,dashboard]
"""
import argparse
import json
import os
import subprocess
import sys


def run(cmd, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=30)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return -1, "", str(e)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--expected-repo", default="tw-trading-unified-git",
                    help="authoritative repo dir name (default: tw-trading-unified-git)")
    ap.add_argument("--expected-sha", default=None,
                    help="deployment manifest SHA; actual HEAD must equal this")
    ap.add_argument("--strict-dirty", action="store_true",
                    help="fail on ANY working-tree change (default: warn only)")
    ap.add_argument("--apps", default="trading-system,dashboard",
                    help="pm2 app names to verify (comma separated)")
    args = ap.parse_args()

    failures = []

    # 1. git top-level
    rc, root, err = run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0 or not root:
        failures.append(f"git rev-parse failed: {err or 'no output'}")
    else:
        repo_name = os.path.basename(root)
        if repo_name != args.expected_repo:
            failures.append(
                f"WRONG REPO: top-level is '{root}' (name='{repo_name}'), "
                f"expected repo name '{args.expected_repo}'"
            )
        else:
            print(f"[OK] repo root: {root}")

    # 2. HEAD
    rc, head, err = run(["git", "rev-parse", "HEAD"], cwd=root if root else None)
    if rc != 0 or not head:
        failures.append(f"git rev-parse HEAD failed: {err or 'no output'}")
    else:
        print(f"[OK] HEAD: {head}")
        if args.expected_sha and head != args.expected_sha:
            failures.append(
                f"WRONG SHA: actual HEAD {head} != expected {args.expected_sha}"
            )
        elif args.expected_sha:
            print(f"[OK] HEAD matches expected deployment SHA")

    # 3. working tree
    rc, porcelain, err = run(["git", "status", "--porcelain"], cwd=root if root else None)
    if rc != 0:
        failures.append(f"git status failed: {err}")
    else:
        changes = [l for l in porcelain.splitlines() if l.strip()]
        if changes:
            msg = f"DIRTY TREE ({len(changes)} change(s)): " + "; ".join(changes[:5])
            if args.strict_dirty:
                failures.append(msg)
            else:
                print(f"[WARN] {msg} (not fatal without --strict-dirty)")
        else:
            print("[OK] working tree clean")

    # 4. pm2 apps
    rc, jlist, err = run(["pm2", "jlist"])
    if rc != 0 or not jlist:
        failures.append(f"pm2 jlist failed: {err or 'no output'}")
    else:
        try:
            apps = json.loads(jlist)
        except json.JSONDecodeError as e:
            failures.append(f"pm2 jlist not JSON: {e}")
            apps = []
        expected_apps = [a for a in args.apps.split(",") if a]
        found = {a.get("name"): a.get("pm2_env", {}) for a in apps}
        for app in expected_apps:
            env = found.get(app)
            if env is None:
                failures.append(f"pm2 app '{app}' not found")
                continue
            exec_path = env.get("pm_exec_path", "")
            cwd = env.get("pm_cwd", "")
            ok = root and (exec_path.startswith(root) and cwd.startswith(root))
            print(f"[{'OK' if ok else 'FAIL'}] {app}: exec={exec_path} cwd={cwd}")
            if not ok:
                failures.append(
                    f"pm2 app '{app}' not under repo '{root}': exec={exec_path} cwd={cwd}"
                )

    print("-" * 60)
    if failures:
        print("RUNTIME MANIFEST GATE: FAIL")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("RUNTIME MANIFEST GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
