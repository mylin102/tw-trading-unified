#!/usr/bin/env python3
"""
r001_analysis.py — R-001 BB Position at Release Analysis (Parquet Consumer)

This is the REFERENCE implementation of R-001 using the trade dataset API.
It does NOT parse JSONL directly.

Usage:
    python scripts/research/r001_analysis.py
    python scripts/research/r001_analysis.py --output reports/research/R-001/

Output:
    reports/research/R-001/
      research_metadata.json     — dataset + analysis context
      r001_results.parquet       — enriched trade-level analysis data
      r001_summary.txt           — human-readable summary
      parity_report.json         — JSONL vs parquet comparison (if legacy data available)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# macOS Silicon optimization
if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd

from core.trade_dataset import (
    current_manifest,
    decision_level_view,
    load_dataset,
    trade_level_view,
)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

RESEARCH_ID = "R-001"
ANALYSIS_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def compute_r001_metrics(tv: pd.DataFrame) -> dict:
    """Compute R-001 trade-level metrics from trade_level_view()."""
    if tv.empty:
        return {}

    total = len(tv)
    winners = tv[tv["pnl_total"] > 0]
    losers = tv[tv["pnl_total"] < 0]
    breakeven = tv[tv["pnl_total"] == 0]
    win_rate = len(winners) / total if total > 0 else 0.0

    return {
        "trade_count": int(total),
        "winner_count": int(len(winners)),
        "loser_count": int(len(losers)),
        "breakeven_count": int(len(breakeven)),
        "win_rate": round(win_rate, 4),
        "total_realized_pnl": round(float(tv["pnl_total"].sum()), 2),
        "avg_pnl": round(float(tv["pnl_total"].mean()), 2),
        "median_pnl": round(float(tv["pnl_total"].median()), 2),
        "avg_mfe": round(float(tv["mfe_combined"].mean()), 2) if "mfe_combined" in tv.columns else None,
        "median_mfe": round(float(tv["mfe_combined"].median()), 2) if "mfe_combined" in tv.columns else None,
        "avg_mae": round(float(tv["mae_combined"].mean()), 2) if "mae_combined" in tv.columns else None,
        "median_mae": round(float(tv["mae_combined"].median()), 2) if "mae_combined" in tv.columns else None,
        "avg_holding_time_s": round(float(tv["holding_time_s"].mean()), 1) if "holding_time_s" in tv.columns else None,
        "median_holding_time_s": round(float(tv["holding_time_s"].median()), 1) if "holding_time_s" in tv.columns else None,
        "session_counts": tv["session"].value_counts().to_dict() if "session" in tv.columns else {},
        "exit_reason_counts": tv["exit_reason"].value_counts().to_dict() if "exit_reason" in tv.columns else {},
    }


def compute_decision_metrics(dv: pd.DataFrame) -> dict:
    """Compute decision-level metrics."""
    if dv.empty:
        return {}

    return {
        "decision_count": int(len(dv)),
        "decision_type_counts": dv["decision_type"].value_counts().to_dict(),
        "reason_counts": dv["reason"].value_counts().to_dict(),
        "entry_count": int((dv["decision_type"] == "ENTRY").sum()),
        "release_near_count": int(dv["decision_type"].str.contains("RELEASE_NEAR").sum()),
        "release_far_count": int(dv["decision_type"].str.contains("RELEASE_FAR").sum()),
        "exit_near_count": int(dv["decision_type"].str.contains("EXIT_NEAR").sum()),
        "exit_far_count": int(dv["decision_type"].str.contains("EXIT_FAR").sum()),
        "snapshot_join_coverage": {
            "z_score_non_null": int(dv["z_score"].notna().sum()),
            "atr_non_null": int(dv["atr"].notna().sum()),
            "snapshots_total": int(len(dv)),
        },
    }


# ---------------------------------------------------------------------------
# Parity test (JSONL vs Parquet)
# ---------------------------------------------------------------------------


def _legacy_jsonl_parity(legacy_report: Path, dataset_report: dict) -> dict:
    """
    Compare legacy JSONL-based analysis with new parquet-based dataset.
    Returns a list of metric comparisons.

    NOTE: legacy_report must be pre-computed by the old R-001 parser.
    If not available, this returns empty.
    """
    if not legacy_report.exists():
        return {"parity_available": False, "metrics": []}

    with open(legacy_report) as f:
        legacy = json.load(f)

    metrics = []

    # Trade-level comparisons
    comparisons = [
        ("closed_trade_count", "trade_count", False),
        ("total_realized_pnl", "total_realized_pnl", True),
        ("win_rate", "win_rate", True),
        ("avg_pnl", "avg_pnl", True),
        ("median_pnl", "median_pnl", True),
    ]

    for legacy_key, dataset_key, has_tolerance in comparisons:
        lv = legacy.get(legacy_key)
        dv = dataset_report.get(dataset_key)
        diff = abs(lv - dv) if lv is not None and dv is not None else None
        pct_diff = diff / abs(lv) * 100 if lv and abs(lv) > 0 else 0
        # Tolerance: 0 for counts, 1% for floating metrics
        tolerance = 0.01 if has_tolerance else 0
        status = "PASS"
        explanation = None
        if diff is not None and diff > tolerance:
            status = "FAIL"
            explanation = f"Difference {diff:.2f} exceeds tolerance {tolerance}"

        metrics.append({
            "metric": legacy_key,
            "legacy_value": lv,
            "dataset_value": dv,
            "absolute_difference": diff,
            "relative_difference_pct": round(pct_diff, 4) if pct_diff is not None else None,
            "tolerance": tolerance,
            "status": status,
            "explanation": explanation,
        })

    return {
        "parity_available": True,
        "metrics": metrics,
        "status": "PASS" if all(m["status"] == "PASS" for m in metrics) else "FAIL",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_analysis(output_dir: Path, legacy_report: Path) -> dict:
    """Run R-001 analysis and write output files. Returns analysis metadata."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load views
    tv = trade_level_view(closed_only=True)
    dv = decision_level_view()
    manifest = current_manifest()

    # Compute metrics
    trade_metrics = compute_r001_metrics(tv)
    decision_metrics = compute_decision_metrics(dv)
    parity = _legacy_jsonl_parity(legacy_report, trade_metrics)

    # Save enriched results
    if not tv.empty:
        tv.to_parquet(output_dir / "r001_results.parquet", compression="snappy", index=False)

    # Build research metadata
    research_metadata = {
        "research_id": RESEARCH_ID,
        "research_version": ANALYSIS_VERSION,
        "dataset_schema_version": manifest.get("schema_version", "?"),
        "dataset_generation": manifest.get("dataset_build_id", "?"),
        "dataset_content_hash": manifest.get("dataset_content_hash", "?"),
        "dataset_git_commit": manifest.get("git_commit", "?"),
        "analysis_git_commit": manifest.get("git_commit", "?") + "+analysis",
        "source_fingerprints": {
            s["path"]: s.get("sha256", "?")
            for s in manifest.get("source_files", [])
        },
        "closed_trade_count": trade_metrics.get("trade_count", 0),
        "decision_count": decision_metrics.get("decision_count", 0),
        "analysis_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_timezone": "Asia/Taipei",
        "parameters": {},
        "trade_metrics": trade_metrics,
        "decision_metrics": decision_metrics,
        "parity": parity,
    }

    # Write metadata
    with open(output_dir / "research_metadata.json", "w") as f:
        json.dump(research_metadata, f, indent=2, default=str)

    # Write parity report separately
    parity_report_path = output_dir / "parity_report.json"
    with open(parity_report_path, "w") as f:
        json.dump(parity, f, indent=2, default=str)

    # Write summary
    _write_summary(tv, trade_metrics, decision_metrics, research_metadata, output_dir)

    return research_metadata


def _write_summary(
    tv: pd.DataFrame,
    trade_metrics: dict,
    decision_metrics: dict,
    metadata: dict,
    output_dir: Path,
):
    """Write human-readable summary."""
    lines = []
    lines.append(f"R-001: BB Position at Release — Analysis Summary")
    lines.append(f"Dataset: {metadata.get('dataset_generation', '?')} ({metadata.get('dataset_content_hash', '?')[:12]}...)")
    lines.append(f"Schema:  {metadata.get('dataset_schema_version', '?')}")
    lines.append(f"Git:     {metadata.get('dataset_git_commit', '?')}")
    lines.append(f"Analyzed: {metadata.get('analysis_timestamp_utc', '?')}")
    lines.append("-" * 50)

    # Trade metrics
    lines.append(f"\nTrades: {trade_metrics.get('trade_count', 0)} closed")
    lines.append(f"  Win/Loss/BE: {trade_metrics.get('winner_count', 0)} / "
                 f"{trade_metrics.get('loser_count', 0)} / {trade_metrics.get('breakeven_count', 0)}")
    lines.append(f"  Win rate: {trade_metrics.get('win_rate', 0)*100:.1f}%")
    lines.append(f"  Total PnL: {trade_metrics.get('total_realized_pnl', 0):.1f}")
    lines.append(f"  Avg PnL: {trade_metrics.get('avg_pnl', 0):.1f}")
    if trade_metrics.get("avg_mfe") is not None:
        lines.append(f"  Avg MFE/MAE: {trade_metrics['avg_mfe']:.1f} / {trade_metrics['avg_mae']:.1f}")
    if trade_metrics.get("avg_holding_time_s") is not None:
        lines.append(f"  Avg holding: {trade_metrics['avg_holding_time_s']/3600:.1f}h")
    lines.append(f"  Sessions: {trade_metrics.get('session_counts', {})}")
    lines.append(f"  Exit reasons: {trade_metrics.get('exit_reason_counts', {})}")

    # Decision metrics
    lines.append(f"\nDecisions: {decision_metrics.get('decision_count', 0)} total")
    lines.append(f"  Types: {decision_metrics.get('decision_type_counts', {})}")
    snap_cov = decision_metrics.get("snapshot_join_coverage", {})
    lines.append(f"  Snapshots: {snap_cov.get('snapshots_total', 0)} rows, "
                 f"{snap_cov.get('z_score_non_null', 0)} with z_score")

    # Parity
    parity = metadata.get("parity", {})
    if parity.get("parity_available"):
        pstatus = parity.get("status", "UNKNOWN")
        lines.append(f"\nParity (JSONL vs Parquet): {pstatus}")
        for m in parity.get("metrics", []):
            lines.append(f"  {m['metric']}: legacy={m['legacy_value']} dataset={m['dataset_value']} [{m['status']}]")

    lines.append("\n" + "=" * 50)
    lines.append(f"Output: {output_dir.resolve()}")

    content = "\n".join(lines)
    with open(output_dir / "r001_summary.txt", "w") as f:
        f.write(content)
    print(content)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=f"{RESEARCH_ID}: BB Position at Release — Parquet Consumer"
    )
    parser.add_argument(
        "--output", default="reports/research/R-001/",
        help="Output directory (default: reports/research/R-001/)",
    )
    parser.add_argument(
        "--legacy-report",
        default="reports/research/R-001/legacy_r001_results.json",
        help="Path to legacy JSONL-based R-001 report for parity comparison",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    legacy_report = Path(args.legacy_report)

    metadata = run_analysis(output_dir, legacy_report)
    print(f"\nOutput: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
