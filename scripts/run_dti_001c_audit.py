#!/usr/bin/env python3
# 2026-07-27 Gemini CLI: DTI-001C Data Quality Audit CLI Runner
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure project root is in sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.futures.mts.data_quality.report_builder import DtiDataQualityReportBuilder
from strategies.futures.mts.data_quality.contracts import GenerationManifest


def load_jsonl_rows(target_path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    files: List[Path] = []
    if target_path.is_file():
        files = [target_path]
    elif target_path.is_dir():
        files = sorted(target_path.rglob("*.jsonl"))

    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except Exception as e:
            print(f"[WARNING] Skipping unreadable file {f}: {e}", file=sys.stderr)
    return rows


def main():
    parser = argparse.ArgumentParser(description="DTI-001C Data Quality & Boundary Audit CLI Runner")
    parser.add_argument("--input", "-i", required=True, help="Path to JSONL file or directory containing captured tick JSONL files.")
    parser.add_argument("--json", action="store_true", help="Output summary as JSON instead of human-readable text.")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input path {input_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    rows = load_jsonl_rows(input_path)
    if not rows:
        print(f"Warning: No valid JSONL rows found under {input_path}", file=sys.stderr)
        sys.exit(0)

    builder = DtiDataQualityReportBuilder()
    summary = builder.build_quality_report(rows)

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("=========================================================")
        print(f"📊 DTI-001C TICK DATA QUALITY AUDIT REPORT")
        print("=========================================================")
        print(f"Generation ID:               {summary.generation_id}")
        print(f"Total Rows Inspected:        {summary.total_rows_inspected}")
        print("---------------------------------------------------------")
        print(f"Overall Rating:              {summary.ratings.overall_rating.value}")
        print(f"Capture Status:              {summary.ratings.capture_status.value}")
        print(f"Execution Gate Blocked:      {summary.ratings.execution_gate_blocked}")
        print("---------------------------------------------------------")
        print("Subsystem Ratings:")
        print(f"  - Monotonicity:            {summary.ratings.subsystem_ratings.monotonicity_rating.value}")
        print(f"  - Duplicates:              {summary.ratings.subsystem_ratings.duplicates_rating.value}")
        print(f"  - Freshness:               {summary.ratings.subsystem_ratings.freshness_rating.value}")
        print(f"  - Boundary Contamination:  {summary.ratings.subsystem_ratings.boundary_rating.value}")
        print("---------------------------------------------------------")
        print("Detailed Metrics:")
        print(f"  - TMF Monotonic Rate:      {summary.near_tick_monotonic_rate * 100:.2f}%")
        print(f"  - TMFI6 Monotonic Rate:    {summary.far_tick_monotonic_rate * 100:.2f}%")
        print(f"  - Micro Reorders:          {summary.micro_reorder_count}")
        print(f"  - Network Reorders:        {summary.network_reorder_count}")
        print(f"  - Severe Regressions:      {summary.severe_regression_count}")
        print(f"  - Exact Duplicates:        {summary.exact_duplicates_count}")
        print(f"  - Semantic Duplicates:     {summary.semantic_duplicates_count}")
        print(f"  - Pair-Skew P50:           {summary.pair_skew_p50_ms:.2f} ms")
        print(f"  - Pair-Skew P90:           {summary.pair_skew_p90_ms:.2f} ms")
        print(f"  - Pair-Skew P99:           {summary.pair_skew_p99_ms:.2f} ms")
        print(f"  - Cross-Gen Contamination: {summary.cross_generation_contamination_count}")
        print("=========================================================")


if __name__ == "__main__":
    main()
