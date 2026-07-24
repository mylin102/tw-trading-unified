#!/usr/bin/env python3
"""
phase2a1_eligibility.py — Phase 2A-1: Replay Case Builder & Eligibility Classifier.

Executes:
  2A-1b  Build 93 cases from decision_level_view()
  2A-1c  Mark 34 RELEASE cases as in scope
  2A-1d  Apply action-specific eligibility rules
  2A-1e  Produce eligibility report + fixed denominator

Output: reports/research/R-002/
  replay_cases.parquet
  eligibility_report.json
  eligibility_cases.parquet
  research_metadata.json
"""

from __future__ import annotations

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

from core.replay_contracts import (
    DecisionReplayCase,
    ReplayEligibility,
    build_replay_cases,
    classify_eligibility,
    eligibility_report,
)
from core.trade_dataset import current_manifest


def main():
    output_dir = Path("reports/research/R-002")
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = current_manifest()
    gen = manifest.get("dataset_build_id", "?")
    ch = manifest.get("dataset_content_hash", "?")
    print(f"Dataset: {gen} ({ch[:16]}...)")

    # 2A-1b: Build 93 cases
    print("\nBuilding replay cases...")
    raw_cases = build_replay_cases()
    print(f"  Raw cases: {len(raw_cases)}")

    if not raw_cases:
        print("ERROR: no cases built")
        sys.exit(1)

    # Verify replay_case_id uniqueness
    ids = [c.replay_case_id for c in raw_cases]
    assert len(ids) == len(set(ids)), "replay_case_id not unique!"

    # 2A-1c: Classify scope
    scope_counts: dict[str, int] = {}
    for c in raw_cases:
        scope_counts[c.scope_label] = scope_counts.get(c.scope_label, 0) + 1
    print(f"\nScope classification:")
    for label, count in sorted(scope_counts.items()):
        print(f"  {label}: {count}")
    in_scope = sum(1 for c in raw_cases if c.in_scope)
    print(f"  IN_SCOPE_RELEASE: {in_scope}")

    # 2A-1d: Apply eligibility rules
    print("\nClassifying eligibility...")
    classified = classify_eligibility(raw_cases)

    # 2A-1e: Eligibility report
    report = eligibility_report(classified)
    print(f"\nEligibility report:")
    print(f"  Total decisions:     {report['total_decisions']}")
    print(f"  In scope (release):  {report['in_scope_release']}")
    print(f"  Eligible:            {report['eligible']}")
    print(f"  Ineligible:          {report['ineligible']}")
    print(f"  Eligibility rate:    {report['eligibility_rate']}%")
    print(f"  Status breakdown:    {report['status_counts']}")
    print(f"  Missing fields:      {report['missing_fields_summary']}")
    print(f"  Reason breakdown:    {report['reason_breakdown']}")

    # Save artifacts
    # 1. replay_cases.parquet — all cases as DataFrame
    case_dicts = [c.to_dict() for c in classified]
    df_cases = pd.DataFrame(case_dicts)
    df_cases.to_parquet(output_dir / "replay_cases.parquet", compression="snappy", index=False)
    print(f"\nSaved: {output_dir}/replay_cases.parquet ({len(df_cases)} rows)")

    # 2. eligibility_cases.parquet — key eligibility fields
    eligibility_rows = []
    for c in classified:
        eligibility_rows.append({
            "replay_case_id": c.replay_case_id,
            "trade_id": c.trade_id,
            "decision_seq": c.decision_seq,
            "decision_type": c.recorded_action,
            "in_scope": c.in_scope,
            "scope_label": c.scope_label,
            "eligibility_status": c.eligibility_status,
            "eligibility_reasons": "; ".join(c.eligibility_reasons),
            "missing_fields": "; ".join(c.missing_fields),
            "recorded_reason": c.recorded_reason,
            "recorded_release_leg": c.recorded_release_leg,
            "snapshot_timing": c.snapshot_timing,
        })
    df_elig = pd.DataFrame(eligibility_rows)
    df_elig.to_parquet(output_dir / "eligibility_cases.parquet", compression="snappy", index=False)
    print(f"Saved: {output_dir}/eligibility_cases.parquet ({len(df_elig)} rows)")

    # 3. eligibility_report.json
    report["dataset_generation"] = gen
    report["dataset_content_hash"] = ch
    report["build_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    with open(output_dir / "eligibility_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"Saved: {output_dir}/eligibility_report.json")

    # 4. research_metadata.json
    metadata = {
        "research_id": "R-002",
        "phase": "2A-1",
        "title": "Release Decision Replay — Eligibility Classification",
        "dataset_generation": gen,
        "dataset_content_hash": ch,
        "dataset_git_commit": manifest.get("git_commit", "?"),
        "analysis_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(classified),
        "in_scope_release": in_scope,
        "eligible": report["eligible"],
        "ineligible": report["ineligible"],
        "eligibility_rate": report["eligibility_rate"],
        "scope_counts": scope_counts,
        "status_counts": report["status_counts"],
        "qualified_denominator": report["eligible"],
        "release_action_match_target": "100%",
        "release_leg_match_target": "100%",
    }
    with open(output_dir / "research_metadata.json", "w") as f:
        json.dump(metadata, f, indent=2, default=str)
    print(f"Saved: {output_dir}/research_metadata.json")

    # Print ineligible cases detail
    ineligible = [c for c in classified if c.eligibility_status != ReplayEligibility.ELIGIBLE.value]
    if ineligible:
        print(f"\n=== Ineligible cases ({len(ineligible)}) ===")
        for c in ineligible:
            print(f"  {c.replay_case_id[:50]:50s} {c.eligibility_status:35s} missing={c.missing_fields}")

    print("\nPhase 2A-1 complete.")


if __name__ == "__main__":
    main()
