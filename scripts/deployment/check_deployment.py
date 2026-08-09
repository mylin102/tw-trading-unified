#!/usr/bin/env python3
"""Deployment Safety Gate — non-applied pre-deploy check (CLI).

Usage:
  python3 scripts/deployment/check_deployment.py \
      --release-dir <repo> \
      --runtime-dir <TRADING_RUNTIME_DIR> \
      --pid-file <pm2 pid file> \
      --position-state <runtime>/logs/position_state.json \
      --monitor strategies/futures/monitor.py \
      --session-generation <n> \
      --margin-available <float> \
      [--expected-sha <40-hex>] [--manifest PHASE1_RC_CANDIDATE.md] [--json]

Exit code 0 = READY (all guards pass); 1 = NOT_READY (refusal codes printed).
The gate NEVER deploys/restarts/unlocks LIVE.
"""

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_CLOSURE = [
    "config/futures.yaml",
    "core/execution_context_state.py",
    "core/release_identity.py",
    "main.py",
    "strategies/futures/monitor.py",
    "strategies/futures/squeeze_futures/data/shioaji_client.py",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--release-dir", default=str(_REPO_ROOT))
    ap.add_argument("--runtime-dir", default=None)
    ap.add_argument("--pid-file", required=True)
    ap.add_argument("--position-state", required=True)
    ap.add_argument("--monitor", default=None)
    ap.add_argument("--session-generation", type=int, default=None)
    ap.add_argument("--margin-available", type=float, default=None)
    ap.add_argument("--expected-sha", default=None)
    ap.add_argument("--manifest", action="append", default=[])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from core.deployment_safety_gate import check_deployment

    manifests = args.manifest or [
        str(_REPO_ROOT / "PHASE1_RC_CANDIDATE.md"),
        str(_REPO_ROOT / "PHASE2_DEPLOYMENT_MANIFEST.md"),
    ]
    check = check_deployment(
        release_dir=args.release_dir,
        closure_files=_CLOSURE,
        runtime_dir=args.runtime_dir,
        pid_file=args.pid_file,
        position_state_path=args.position_state,
        monitor_path=args.monitor
        or str(_REPO_ROOT / "strategies/futures/monitor.py"),
        session_generation=args.session_generation,
        margin_available=args.margin_available,
        manifest_paths=manifests,
        expected_sha=args.expected_sha,
    )
    if args.json:
        print(json.dumps({
            "ok": check.ok,
            "refusal_codes": list(check.refusal_codes),
            "guards": [
                {"guard": g.guard, "ok": g.ok, "reasons": list(g.reasons),
                 "detail": g.detail} for g in check.results
            ],
        }, indent=2, ensure_ascii=False))
    else:
        for g in check.results:
            mark = "PASS" if g.ok else "FAIL"
            print(f"[{mark}] {g.guard}: {g.reasons or 'ok'}"
                  f"{'  ' + g.detail if g.detail else ''}")
        print("READY" if check.ok else
              f"NOT_READY refusal_codes={list(check.refusal_codes)}")
    return 0 if check.ok else 1


if __name__ == "__main__":
    sys.exit(main())
