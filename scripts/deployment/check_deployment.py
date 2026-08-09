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
    ap.add_argument("--margin-evidence", default=None,
                    help="path to the read-only preflight JSON carrying "
                         "account_identity_hash/scope/captured_at/"
                         "canonical_input_hash (bare --margin-available "
                         "is insufficient)")
    ap.add_argument("--expected-sha", default=None)
    ap.add_argument("--manifest", action="append", default=[])
    ap.add_argument("--exclude-path", action="append", default=[])
    ap.add_argument("--phase", choices=["pre_deploy", "post_startup"],
                    default="pre_deploy")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from core.deployment_safety_gate import check_deployment

    margin_evidence = None
    if args.margin_evidence:
        import json as _json
        with open(args.margin_evidence, encoding="utf-8") as _fh:
            _ev = _json.load(_fh)
        margin_evidence = {k: _ev.get(k) for k in (
            "account_identity_hash", "scope", "captured_at",
            "canonical_input_hash")}

    manifests = args.manifest or [
        str(_REPO_ROOT / "PHASE1_RC_CANDIDATE.md"),
        str(_REPO_ROOT / "PHASE2_DEPLOYMENT_MANIFEST.md"),
        str(_REPO_ROOT / "PHASE1_FINAL_FREEZE.md"),
    ]
    # exclude-self: the manifest/rollback docs are excluded from the tree
    # identity (recording the freeze must not invalidate itself)
    exclude = args.exclude_path or [
        "PHASE1_RC_CANDIDATE.md", "PHASE2_DEPLOYMENT_MANIFEST.md",
        "PHASE1_FINAL_FREEZE.md",
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
        margin_evidence=margin_evidence,
        manifest_paths=manifests,
        manifest_exclude_paths=exclude,
        expected_sha=args.expected_sha,
        phase=args.phase,
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
        if check.ok:
            # D3: explicit phase-specific verdict — pre_deploy READY is
            # READY_FOR_STARTUP (never to be confused with a deploy READY);
            # machine-readable phase= label for the deployment script
            if args.phase == "pre_deploy":
                print("phase=READY_FOR_STARTUP")
            else:
                print("phase=READY")
        else:
            print(f"phase=NOT_READY refusal_codes={list(check.refusal_codes)}")
        # D6: stock-account rows stay warning/evidence (never block MTS
        # futures flat unless global-risk policy)
        flat = next((g for g in check.results if g.guard == "flat_snapshot"),
                    None)
        if flat is not None and "stock row" in flat.detail:
            print(f"WARNING {flat.detail}")
    return 0 if check.ok else 1


if __name__ == "__main__":
    sys.exit(main())
