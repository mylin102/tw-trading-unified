#!/usr/bin/env python3
"""
phase3a_sensitivity.py — Phase 3A: Single-variable sensitivity analysis.

Replays 34 RELEASE cases across 8 release_threshold levels.
Produces:
  - experiment_results.parquet (272 rows)
  - decision_boundary.parquet
  - per_level_summary.parquet
  - experiment_report.json
  - sensitivity_summary.txt

Usage:
    python scripts/research/phase3a_sensitivity.py
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

from core.experiment import (
    ExperimentConfig,
    analyze_experiment,
    build_report,
    run_experiment,
    save_experiment_results,
)
from core.replay_contracts import build_replay_cases, classify_eligibility
from core.trade_dataset import current_manifest


def main():
    output_dir = Path("reports/research/R-003")
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = current_manifest()
    gen = manifest.get("dataset_build_id", "?")
    ch = manifest.get("dataset_content_hash", "?")

    # Build cases
    print("Loading cases...")
    raw_cases = build_replay_cases()
    classified = classify_eligibility(raw_cases)
    release_cases = [c for c in classified if c.recorded_action.startswith("RELEASE") and c.in_scope]
    print(f"  Eligible RELEASE cases: {len(release_cases)}")

    # Phase 3A: release_threshold sensitivity
    config = ExperimentConfig(
        parameter="release_threshold",
        levels=[6, 8, 10, 12, 14, 16, 18, 20],
        label="Release Stop Threshold Sensitivity",
    )

    experiment_id = f"R-003A_{datetime.now().strftime('%Y%m%dT%H%M%S')}"

    # Run
    results = run_experiment(release_cases, config, experiment_id)

    # Analyze
    print("\nAnalyzing...")
    analysis = analyze_experiment(results)

    # Report
    metadata = {
        "dataset_generation": gen,
        "dataset_content_hash": ch,
        "analysis_timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    report = build_report(results, config, analysis, experiment_id, metadata)

    # Save
    save_experiment_results(results, analysis, report, output_dir)

    print(f"\nExperiment {experiment_id} complete.")
    print(f"Output: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
