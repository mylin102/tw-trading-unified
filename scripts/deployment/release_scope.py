#!/usr/bin/env python3
"""Select and verify PM2 apps belonging to one canonical release cohort."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


class ReleaseScopeError(RuntimeError):
    pass


def plan_auxiliary_repin(pm2_rows: list[dict], names: list[str]) -> list[dict]:
    """Plan auxiliary re-registration without reviving intentionally stopped apps."""
    live = {str(row.get("name")): row for row in pm2_rows if isinstance(row, dict)}
    plan: list[dict] = []
    for name in names:
        row = live.get(str(name)) or {}
        status = str((row.get("pm2_env") or {}).get("status") or "missing")
        if status == "stopped":
            plan.append({"name": str(name), "action": "preserve-stopped"})
        elif status == "online":
            plan.append({"name": str(name), "action": "repin"})
        else:
            raise ReleaseScopeError(
                f"auxiliary app {name} has unsupported pre-deploy status={status}")
    return plan


def verify_auxiliary_release_apps(
    pm2_rows: list[dict], before_rows: list[dict], names: list[str], expected_sha: str
) -> None:
    """Verify stopped apps stayed stopped and online apps were re-pinned online."""
    after = {str(row.get("name")): row for row in pm2_rows if isinstance(row, dict)}
    before = {str(row.get("name")): row for row in before_rows if isinstance(row, dict)}
    failures: list[str] = []
    for name in names:
        pre = (before.get(str(name)) or {}).get("pm2_env") or {}
        post = (after.get(str(name)) or {}).get("pm2_env") or {}
        pre_status = str(pre.get("status") or "missing")
        post_status = str(post.get("status") or "missing")
        actual = str(post.get("LRC_RELEASE_SHA") or "<missing>")
        if pre_status == "stopped":
            if post_status != "stopped":
                failures.append(
                    f"{name}(preserved-stopped violated: status={post_status})")
        elif pre_status == "online":
            if post_status != "online" or actual != expected_sha:
                failures.append(
                    f"{name}(must-be-online status={post_status},actual_sha={actual},"
                    f"expected_sha={expected_sha})")
        else:
            failures.append(f"{name}(unsupported pre-deploy status={pre_status})")
    if failures:
        raise ReleaseScopeError("auxiliary release verification failed: " + ", ".join(failures))


def _inside(path: object, root: str) -> bool:
    if not isinstance(path, str) or not path:
        return False
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def select_release_apps(
    ecosystem_apps: list[dict], pm2_rows: list[dict], release_root: str, primary: str
) -> list[str]:
    """Return the pinned apps sharing the primary app's current release lineage.

    cwd alone is insufficient: stock-runner can execute under the canonical cwd
    while retaining an independently deployed SHA.  Equality with the primary
    app's pre-deploy SHA is the lineage boundary.
    """
    live = {str(row.get("name")): row for row in pm2_rows if isinstance(row, dict)}
    primary_env = (live.get(primary) or {}).get("pm2_env") or {}
    baseline_sha = str(primary_env.get("LRC_RELEASE_SHA") or "")
    selected: list[str] = []
    for app in ecosystem_apps:
        if not isinstance(app, dict):
            continue
        name = str(app.get("name") or "")
        env = app.get("env") or {}
        row_env = (live.get(name) or {}).get("pm2_env") or {}
        if not name or not env.get("LRC_RELEASE_SHA"):
            continue
        if not _inside(app.get("cwd"), release_root):
            continue
        if not _inside(row_env.get("pm_cwd"), release_root):
            continue
        current_sha = str(row_env.get("LRC_RELEASE_SHA") or "")
        if name != primary and (not baseline_sha or current_sha != baseline_sha):
            continue
        selected.append(name)
    if primary not in selected:
        raise ReleaseScopeError(f"primary app is outside selected release scope: {primary}")
    return selected


def verify_release_apps(pm2_rows: list[dict], names: list[str], expected_sha: str) -> None:
    live = {str(row.get("name")): row for row in pm2_rows if isinstance(row, dict)}
    failures: list[str] = []
    for name in names:
        env = (live.get(name) or {}).get("pm2_env") or {}
        actual = str(env.get("LRC_RELEASE_SHA") or "<missing>")
        status = str(env.get("status") or "missing")
        if actual != expected_sha or status != "online":
            failures.append(
                f"{name}(status={status},actual_sha={actual},expected_sha={expected_sha})"
            )
    if failures:
        raise ReleaseScopeError("release scope verification failed: " + ", ".join(failures))


def _read(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select")
    select.add_argument("--ecosystem-json", required=True)
    select.add_argument("--pm2-json", required=True)
    select.add_argument("--release-root", required=True)
    select.add_argument("--primary", required=True)
    select.add_argument("--out", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--scope", required=True)
    verify.add_argument("--pm2-json", required=True)
    verify.add_argument("--expected-sha", required=True)
    plan = sub.add_parser("plan-aux")
    plan.add_argument("--scope", required=True)
    plan.add_argument("--pm2-json", required=True)
    plan.add_argument("--primary", required=True)
    verify_aux = sub.add_parser("verify-aux")
    verify_aux.add_argument("--scope", required=True)
    verify_aux.add_argument("--before-pm2-json", required=True)
    verify_aux.add_argument("--pm2-json", required=True)
    verify_aux.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        if args.command == "select":
            apps = select_release_apps(
                _read(args.ecosystem_json), _read(args.pm2_json),
                args.release_root, args.primary,
            )
            pm2 = {str(row.get("name")): row for row in _read(args.pm2_json)}
            baseline = str((pm2[args.primary].get("pm2_env") or {})["LRC_RELEASE_SHA"])
            payload = {"release_root": str(Path(args.release_root).resolve()),
                       "primary": args.primary, "baseline_sha": baseline, "apps": apps}
            tmp = args.out + ".tmp"
            Path(tmp).write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, args.out)
            print(" ".join(apps))
        elif args.command == "verify":
            scope = _read(args.scope)
            verify_release_apps(_read(args.pm2_json), list(scope.get("apps") or []),
                                args.expected_sha)
            print("release scope identity OK: " + " ".join(scope["apps"]))
        elif args.command == "plan-aux":
            scope = _read(args.scope)
            names = [name for name in scope.get("apps") or []
                     if name != args.primary]
            for item in plan_auxiliary_repin(_read(args.pm2_json), names):
                print(f"{item['name']}\t{item['action']}")
        else:
            scope = _read(args.scope)
            names = [name for name in scope.get("apps") or []
                     if name != scope.get("primary")]
            verify_auxiliary_release_apps(
                _read(args.pm2_json), _read(args.before_pm2_json), names,
                args.expected_sha)
            print("auxiliary release state OK: " + " ".join(names))
    except (KeyError, OSError, ValueError, ReleaseScopeError) as exc:
        print(f"[RELEASE_SCOPE] FAIL: {exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
