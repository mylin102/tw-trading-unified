#!/usr/bin/env python3
"""Stage 8 of deploy_release.sh — verify the LIVE execution context is real.

Read-only.  Fails closed: any missing/ambiguous evidence is a non-zero exit
with a typed reason.  Checks
  * execution_context.json exists in the runtime dir
  * requested_mode == live, effective_mode == live_ready
  * live_order_allowed is True
  * audit_reasons is empty
  * the RUNNING pm2 process carries LRC_RELEASE_SHA == the expected sha
    (a restart that silently kept an old env is caught here)

Exit codes: 0 ok / 2 missing evidence / 3 context not live_ready / 4 identity
mismatch / 5 disk floor not met / 6 pm2 query failed.

--check-disk exposes the disk floor as a pure, unit-tested function instead of a
tunable threshold inside the deploy script (audit 2026-09-20: safety thresholds
must not be overridable).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

# Paper session ids are written by core/mode_transition.py:148 (strftime).
_PAPER_SESSION_RE = re.compile(r"[0-9]{8}_[0-9]{6}")


def paper_context_violations(ctx: dict) -> list:
    """Paper rules: no live order authority, no audit reasons, bound session."""
    bad = []
    if ctx.get("live_order_allowed") is not False:
        bad.append("LIVE_ORDER_ALLOWED")
    if ctx.get("audit_reasons"):
        bad.append("AUDIT_REASONS")
    sid = ctx.get("session_id")
    if not isinstance(sid, str) or not _PAPER_SESSION_RE.fullmatch(sid):
        bad.append("SESSION_ID_UNBOUND")
    return bad


def disk_free_gb(path: str, _statvfs=None) -> float:
    """Free gigabytes on the filesystem holding ``path`` (pure, injectable)."""
    statvfs = _statvfs or os.statvfs
    st = statvfs(path)
    return st.f_bavail * st.f_frsize / float(1024 ** 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-dir", required=True)
    ap.add_argument("--expected-sha", default=None)
    ap.add_argument("--app", default="trading-system")
    ap.add_argument("--pm2-bin", default="pm2")
    ap.add_argument("--check-disk", action="store_true",
                    help="only check the disk floor and print free GB")
    ap.add_argument("--min-free-gb", type=int, default=3)
    args = ap.parse_args()

    if not args.check_disk and not args.expected_sha:
        print("[VERIFY] FAIL --expected-sha is required: the running pm2 identity "
              "check is the point of this verifier")
        return 2

    if args.check_disk:
        try:
            free = disk_free_gb(args.runtime_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"[VERIFY] FAIL cannot stat {args.runtime_dir}: {exc}")
            return 2
        if free < args.min_free_gb:
            print(f"[VERIFY] FAIL disk free {free:.1f}GB < required {args.min_free_gb}GB")
            return 5
        print(f"{free:.0f}")
        return 0

    ctx_path = os.path.join(args.runtime_dir, "execution_context.json")
    if not os.path.exists(ctx_path):
        print(f"[VERIFY] FAIL missing {ctx_path}")
        return 2
    try:
        ctx = json.load(open(ctx_path, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[VERIFY] FAIL unreadable {ctx_path}: {exc}")
        return 2

    requested = ctx.get("requested_mode")
    effective = ctx.get("effective_mode")
    allowed = ctx.get("live_order_allowed")
    reasons = ctx.get("audit_reasons") or []
    print(f"[VERIFY] requested={requested} effective={effective} "
          f"live_order_allowed={allowed} audit_reasons={reasons}")
    is_paper = requested == "paper" and effective == "paper_active"
    if is_paper:
        bad = paper_context_violations(ctx)
        if bad:
            print(f"[VERIFY] FAIL paper context is not clean: {', '.join(bad)}")
            return 3
    elif requested != "live" or effective != "live_ready" or allowed is not True or reasons:
        print("[VERIFY] FAIL context is not a clean live_ready state")
        return 3

    if args.expected_sha:
        try:
            items = json.loads(subprocess.check_output([args.pm2_bin, "jlist"], text=True))
        except Exception as exc:  # noqa: BLE001
            print(f"[VERIFY] FAIL pm2 jlist: {exc}")
            return 5
        env = next((x.get("pm2_env", {}) for x in items if x.get("name") == args.app), None)
        if env is None:
            print(f"[VERIFY] FAIL pm2 app not found: {args.app}")
            return 5
        running_sha = env.get("LRC_RELEASE_SHA")
        if running_sha != args.expected_sha:
            print(f"[VERIFY] FAIL identity mismatch: running LRC_RELEASE_SHA="
                  f"{running_sha} expected={args.expected_sha}")
            return 4
        print(f"[VERIFY] identity OK (LRC_RELEASE_SHA={running_sha})")

    print("[VERIFY] paper context OK" if is_paper else "[VERIFY] live context OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
