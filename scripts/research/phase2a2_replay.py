#!/usr/bin/env python3
"""
phase2a2_replay.py — Phase 2A-2: Side-effect-free Release Decision Replay.

Replays 34 RELEASE cases against production evaluate_lifecycle_actions().
No orders, no state files, no JSONL writes.

Usage:
    python scripts/research/phase2a2_replay.py
    python scripts/research/phase2a2_replay.py --smoke   # single-case smoke test
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd

from core.replay_contracts import build_replay_cases, classify_eligibility
from core.replay_release import (
    MismatchCategory,
    build_reproduction_report,
    order_independence_check,
    replay_batch,
    replay_single_release,
    side_effect_check,
)
from core.trade_dataset import current_manifest


def _canonical_hash(results: list) -> str:
    """Compute deterministic hash over replay results (excluding timestamps)."""
    h = hashlib.sha256()
    for r in results:
        row = f"{r.replay_case_id}:{r.replayed_action}:{r.replayed_release_leg}:{r.mismatch_category}"
        h.update(row.encode())
    return h.hexdigest()


def main():
    output_dir = Path("reports/research/R-002")
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = current_manifest()
    gen = manifest.get("dataset_build_id", "?")
    ch = manifest.get("dataset_content_hash", "?")

    smoke = "--smoke" in sys.argv

    print(f"Dataset: {gen} ({ch[:16]}...)")
    print(f"Mode: {'SMOKE (single case)' if smoke else 'BATCH (all cases)'}")

    # Build & classify cases
    print("\nBuilding replay cases...")
    raw_cases = build_replay_cases()
    classified = classify_eligibility(raw_cases)
    release_cases = [c for c in classified if c.recorded_action.startswith("RELEASE") and c.in_scope]
    print(f"  Eligible RELEASE cases: {len(release_cases)}")

    if not release_cases:
        print("ERROR: no eligible release cases")
        sys.exit(1)

    # Smoke test: single case first
    if smoke:
        case = release_cases[0]
        print(f"\n=== Smoke test: {case.replay_case_id} ===")
        print(f"  Action: {case.recorded_action}")
        print(f"  Leg:    {case.recorded_release_leg}")
        print(f"  Reason: {case.recorded_reason}")
        print(f"  ATR:    {case.atr}")
        print(f"  Threshold: {case.release_stop_threshold}")
        print(f"  Near entry: {case.near_pnl}  Near price: {case.near_price}")
        print(f"  Far entry:  {case.far_pnl}  Far price: {case.far_price}")

        result = replay_single_release(case)
        print(f"\n  Result:")
        print(f"    Replayed: {result.replayed_action} leg={result.replayed_release_leg} reason={result.replayed_reason}")
        print(f"    Action match: {result.action_match}")
        print(f"    Leg match:    {result.leg_match}")
        print(f"    Reason match: {result.reason_match}")
        print(f"    Category:     {result.mismatch_category}")
        for a in result.lifecycle_assumptions:
            print(f"    Assumption: {a}")

        if result.exception_type:
            print(f"    EXCEPTION: {result.exception_type}: {result.exception_msg}")

        return

    # --- Batch replay ---
    side_before = side_effect_check()

    print(f"\nReplaying {len(release_cases)} RELEASE cases...")
    results = replay_batch(release_cases)

    side_after = side_effect_check()

    # Report
    report = build_reproduction_report(results)
    print(f"\n=== Reproduction Report ===")
    print(f"  Total:          {report['total_cases']}")
    print(f"  Action match:   {report['action_match']}/{report['total_cases']} ({report['action_match_rate']}%)")
    print(f"  Leg match:      {report['leg_match']}/{report['total_cases']} ({report['leg_match_rate']}%)")
    print(f"  Reason match:   {report['reason_match']}/{report['total_cases']} ({report['reason_match_rate']}%)")
    print(f"  Mismatches:     {report['mismatch_count']} ({report['mismatch_rate']}%)")
    print(f"  Category breakdown: {report['category_counts']}")
    print(f"  Exceptions:     {report['exception_count']}")

    # Side effect check
    print(f"\n=== Side Effect Check ===")
    no_side_effects = (
        side_before["state_file_exists"] == side_after["state_file_exists"]
        and side_before["fills_log_lines"] == side_after["fills_log_lines"]
    )
    print(f"  State file: {side_before['state_file_exists']} → {side_after['state_file_exists']}")
    print(f"  Fills log:  {side_before['fills_log_lines']} → {side_after['fills_log_lines']}")
    print(f"  Side effects: {'NONE ✓' if no_side_effects else 'DETECTED ✗'}")

    # Order independence
    oi = order_independence_check(release_cases)
    print(f"\n=== Order Independence ===")
    print(f"  Forward: {oi['forward_count']} cases")
    print(f"  Reverse: {oi['reverse_count']} cases")
    print(f"  Independent: {oi['order_independent']}")
    if not oi['order_independent']:
        print(f"  Mismatches: {oi['mismatch_count']}")

    # Content hash
    content_hash = _canonical_hash(results)
    print(f"\n  Reproduction content hash: {content_hash[:16]}...")

    # --- Save results ---
    # reproduction_results.parquet
    result_dicts = []
    for r in results:
        result_dicts.append({
            "replay_case_id": r.replay_case_id,
            "trade_id": r.trade_id,
            "decision_seq": r.decision_seq,
            "recorded_action": r.recorded_action,
            "recorded_release_leg": r.recorded_release_leg,
            "recorded_reason": r.recorded_reason,
            "recorded_threshold": r.recorded_threshold,
            "replayed_action": r.replayed_action,
            "replayed_release_leg": r.replayed_release_leg,
            "replayed_reason": r.replayed_reason,
            "replayed_threshold": r.replayed_threshold,
            "action_match": r.action_match,
            "leg_match": r.leg_match,
            "reason_match": r.reason_match,
            "mismatch_category": r.mismatch_category,
            "lifecycle_state_source": r.lifecycle_state_source,
            "lifecycle_assumptions": "; ".join(r.lifecycle_assumptions) if r.lifecycle_assumptions else "",
            "exception_type": r.exception_type,
            "exception_msg": r.exception_msg,
        })
    df_results = pd.DataFrame(result_dicts)
    df_results.to_parquet(output_dir / "reproduction_results.parquet", compression="snappy", index=False)
    print(f"\nSaved: {output_dir}/reproduction_results.parquet ({len(df_results)} rows)")

    # reproduction_mismatches.parquet
    mismatches = [r for r in result_dicts if r["mismatch_category"] != MismatchCategory.MATCH.value]
    if mismatches:
        df_mm = pd.DataFrame(mismatches)
        df_mm.to_parquet(output_dir / "reproduction_mismatches.parquet", compression="snappy", index=False)
        print(f"Saved: {output_dir}/reproduction_mismatches.parquet ({len(df_mm)} rows)")

    # reproduction_report.json
    report["dataset_generation"] = gen
    report["dataset_content_hash"] = ch
    report["build_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    report["side_effects_detected"] = not no_side_effects
    report["order_independent"] = oi["order_independent"]
    report["reproduction_content_hash"] = content_hash
    report["analysis_git_commit"] = _get_git_commit()
    with open(output_dir / "reproduction_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"Saved: {output_dir}/reproduction_report.json")

    # Print mismatches
    if mismatches:
        print(f"\n=== Mismatches ({len(mismatches)}) ===")
        for m in mismatches:
            print(f"  {m['trade_id']} seq={m['decision_seq']}: "
                  f"recorded={m['recorded_action']}/{m['recorded_release_leg']} "
                  f"replayed={m['replayed_action']}/{m['replayed_release_leg']} "
                  f"cat={m['mismatch_category']}")

    print(f"\nPhase 2A-2 complete.")


def _get_git_commit() -> str:
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                        stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


if __name__ == "__main__":
    main()
