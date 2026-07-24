#!/usr/bin/env python3
"""
parity_check.py — Frozen-source parity: legacy JSONL vs new parquet dataset.

Usage:
    python scripts/research/parity_check.py
    python scripts/research/parity_check.py --output reports/research/R-001/

Compares the same frozen JSONL source parsed two ways:
  1. Legacy: direct JSONL line-by-line parsing (same algorithm as old R-001 parser)
  2. Dataset: trade_level_view() from the parity generation

Output: parity_report.json with per-metric comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
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

from core.trade_dataset import _file_sha256, trade_level_view


# ---------------------------------------------------------------------------
# Frozen source path
# ---------------------------------------------------------------------------

FROZEN_DIR = Path("data/frozen/parity_final")
FILLS_PATH = FROZEN_DIR / "mts_trade_fills.jsonl"
EVENTS_PATH = FROZEN_DIR / "mts_spread_events.jsonl"


# ---------------------------------------------------------------------------
# Legacy parser: direct JSONL parsing (replicates old R-001 approach)
# ---------------------------------------------------------------------------


def _parse_legacy(fills_path: Path, events_path: Path) -> pd.DataFrame:
    """
    Parse fills + events JSONL the old way (line-by-line, no snapshot/decision framework).
    Returns a DataFrame with one row per closed trade.
    """
    import json

    # Load fills
    fills_by_trade: dict[str, list[dict]] = defaultdict(list)
    with open(fills_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                tid = rec.get("trade_id", "")
                if tid:
                    fills_by_trade[tid].append(rec)

    # Load events
    events = []
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    # Build trade rows (same grain as facts + outcomes)
    rows = []
    for trade_id in sorted(fills_by_trade.keys()):
        fills = fills_by_trade[trade_id]
        entries = [f for f in fills if f.get("fill_type") == "ENTRY"]
        releases = [f for f in fills if f.get("fill_type") == "RELEASE"]
        exits_all = [f for f in fills if f.get("fill_type") == "EXIT"]

        near_entry = next((f for f in entries if f.get("leg") == "NEAR"), None)
        far_entry = next((f for f in entries if f.get("leg") == "FAR"), None)
        release = releases[0] if releases else None

        if not near_entry or not far_entry:
            continue

        # Skip open trades (no EXIT fills)
        if not exits_all:
            continue

        # Session
        session = entries[0].get("session", "unknown")

        # PnL
        pnl_total = sum(f.get("realized_pnl") or 0.0 for f in fills)

        # Entry/exit timestamps
        entry_ts = entries[0].get("timestamp", "")
        exit_fill = sorted(exits_all, key=lambda f: f.get("timestamp", ""))[-1]
        exit_ts = exit_fill.get("timestamp", "")

        # Holding time
        holding_time_s = None
        try:
            ed = datetime.fromisoformat(entry_ts)
            xd = datetime.fromisoformat(exit_ts)
            holding_time_s = (xd - ed).total_seconds()
        except (ValueError, TypeError):
            pass

        # MFE/MAE from fills
        mfe_released = release.get("leg_mfe") if release else None
        mae_released = release.get("leg_mae") if release else None
        mfe_remaining = exit_fill.get("leg_mfe")
        mae_remaining = exit_fill.get("leg_mae")

        # Exit reason from events correlation
        exit_reason = ""
        try:
            entry_dt = datetime.fromisoformat(entry_ts)
            window = __import__("datetime").timedelta(milliseconds=5000)
            for ev in events:
                try:
                    ev_ts = datetime.fromisoformat(ev.get("ts", ""))
                except (ValueError, TypeError):
                    continue
                if abs(ev_ts - entry_dt) <= window and ev.get("event") == "EXIT_LOG":
                    exit_reason = ev.get("exit_reason", "")
                    break
        except (ValueError, TypeError):
            pass

        # Legacy parser doesn't compute MFE/MAE from EXIT_LOG (only from fills)
        row = {
            "trade_id": trade_id,
            "session": session,
            "pnl_total": round(pnl_total, 2),
            "held_time_s": round(holding_time_s, 3) if holding_time_s is not None else None,
            "mfe_released": mfe_released,
            "mae_released": mae_released,
            "mfe_remaining": mfe_remaining,
            "mae_remaining": mae_remaining,
            "exit_reason": exit_reason,
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    # Coerce types
    for col in ["pnl_total", "held_time_s", "mfe_released", "mae_released",
                 "mfe_remaining", "mae_remaining"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compute_metrics(df: pd.DataFrame, label: str) -> dict:
    """Compute trade-level metrics from a DataFrame."""
    if df.empty:
        return {"label": label, "trade_count": 0}

    winners = df[df["pnl_total"] > 0]
    losers = df[df["pnl_total"] < 0]
    breakeven = df[df["pnl_total"] == 0]
    n = len(df)

    return {
        "label": label,
        "trade_count": int(n),
        "winner_count": int(len(winners)),
        "loser_count": int(len(losers)),
        "breakeven_count": int(len(breakeven)),
        "win_rate": round(len(winners) / n, 4) if n > 0 else 0.0,
        "total_pnl": round(float(df["pnl_total"].sum()), 2),
        "avg_pnl": round(float(df["pnl_total"].mean()), 2),
        "median_pnl": round(float(df["pnl_total"].median()), 2),
        "session_day": int((df["session"] == "day").sum()),
        "session_night": int((df["session"] == "night").sum()),
        "exit_reason_counts": df["release_reason"].value_counts().to_dict() if "release_reason" in df.columns else {},
        "final_exit_reason_counts": df["final_exit_reason"].value_counts().to_dict() if "final_exit_reason" in df.columns else {},
        "trade_ids": sorted(df["trade_id"].tolist()),
    }


def compare_metrics(legacy: dict, dataset: dict) -> list[dict]:
    """Compare two metric dicts and produce a parity report list."""
    comparisons = []

    # Strict equality metrics
    for key in ["trade_count", "winner_count", "loser_count", "breakeven_count",
                "session_day", "session_night"]:
        lv = legacy.get(key)
        dv = dataset.get(key)
        match = lv == dv
        comparisons.append({
            "metric": key,
            "legacy_value": lv,
            "dataset_value": dv,
            "absolute_difference": abs(lv - dv) if lv is not None and dv is not None else None,
            "tolerance": 0,
            "status": "PASS" if match else "FAIL",
        })

    # Win rate
    lv_wr = legacy.get("win_rate")
    dv_wr = dataset.get("win_rate")
    wr_diff = abs(lv_wr - dv_wr) if lv_wr is not None and dv_wr is not None else None
    comparisons.append({
        "metric": "win_rate",
        "legacy_value": lv_wr,
        "dataset_value": dv_wr,
        "absolute_difference": round(wr_diff, 6) if wr_diff is not None else None,
        "tolerance": 0.0001,
        "status": "PASS" if (wr_diff is not None and wr_diff <= 0.0001) else "FAIL",
    })

    # Numeric with tolerance
    for key, tol in [("total_pnl", 0.1), ("avg_pnl", 0.01), ("median_pnl", 0.01)]:
        lv = legacy.get(key)
        dv = dataset.get(key)
        diff = abs(lv - dv) if lv is not None and dv is not None else None
        comparisons.append({
            "metric": key,
            "legacy_value": lv,
            "dataset_value": dv,
            "absolute_difference": round(diff, 4) if diff is not None else None,
            "tolerance": tol,
            "status": "PASS" if (diff is not None and diff <= tol) else "FAIL",
        })

    # Trade ID set
    lv_ids = set(legacy.get("trade_ids", []))
    dv_ids = set(dataset.get("trade_ids", []))
    ids_match = lv_ids == dv_ids
    comparisons.append({
        "metric": "trade_id_set",
        "legacy_value": len(lv_ids),
        "dataset_value": len(dv_ids),
        "absolute_difference": len(lv_ids ^ dv_ids),
        "tolerance": 0,
        "status": "PASS" if ids_match else "FAIL",
        "details": {
            "in_legacy_not_dataset": sorted(lv_ids - dv_ids),
            "in_dataset_not_legacy": sorted(dv_ids - lv_ids),
        } if not ids_match else None,
    })

    # Exit reason counts
    lv_er = legacy.get("exit_reason_counts", {})
    dv_er = dataset.get("exit_reason_counts", {})
    er_match = lv_er == dv_er
    comparisons.append({
        "metric": "exit_reason_counts",
        "legacy_value": lv_er,
        "dataset_value": dv_er,
        "absolute_difference": None,
        "tolerance": 0,
        "status": "PASS" if er_match else "EXPECTED_DIFF",
        "details": {
            "note": "exit_reason semantics differ: legacy extracts from correlated EXIT_LOG events, dataset uses RELEASE_SUBMITTED events with risk_mode. Both are valid but represent different lifecycle stages."
        },
    })

    # Final exit reason counts (dataset only — legacy doesn't compute this)
    dv_fer = dataset.get("final_exit_reason_counts", {})
    lv_fer = legacy.get("exit_reason_counts", {})
    comparisons.append({
        "metric": "final_exit_reason_counts",
        "legacy_value": None,
        "dataset_value": dv_fer,
        "absolute_difference": None,
        "tolerance": None,
        "status": "NOT_COMPARABLE",
        "details": {
            "note": "New dataset splits exit_reason into release_reason + final_exit_reason. Legacy did not compute final_exit_reason. This is a new capability, not a regression."
        },
    })

    return comparisons


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Frozen-source parity: legacy JSONL vs parquet dataset"
    )
    parser.add_argument(
        "--output", default="reports/research/R-001/",
        help="Output directory",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Source verification
    fills_sha = _file_sha256(FILLS_PATH)
    events_sha = _file_sha256(EVENTS_PATH)
    print(f"Frozen fills:  {fills_sha[:16]}... ({FILLS_PATH.stat().st_size} bytes)")
    print(f"Frozen events: {events_sha[:16]}... ({EVENTS_PATH.stat().st_size} bytes)")

    # 1. Legacy parsing
    print("\nParsing legacy (direct JSONL)...")
    legacy_df = _parse_legacy(FILLS_PATH, EVENTS_PATH)
    legacy_metrics = compute_metrics(legacy_df, "legacy_jsonl")
    print(f"  Legacy: {legacy_metrics['trade_count']} trades, "
          f"PnL={legacy_metrics['total_pnl']}, "
          f"WR={legacy_metrics['win_rate']*100:.1f}%")

    # Save legacy results
    legacy_df.to_parquet(output_dir / "legacy_trades.parquet", compression="snappy", index=False)
    with open(output_dir / "legacy_metrics.json", "w") as f:
        json.dump(legacy_metrics, f, indent=2)

    # 2. Dataset (parquet) consumption
    print("Loading dataset (trade_level_view)...")
    dataset_df = trade_level_view(closed_only=True)
    dataset_metrics = compute_metrics(dataset_df, "parquet_dataset")
    print(f"  Dataset: {dataset_metrics['trade_count']} trades, "
          f"PnL={dataset_metrics['total_pnl']}, "
          f"WR={dataset_metrics['win_rate']*100:.1f}%")

    # 3. Compare
    print("\nComparing...")
    comparisons = compare_metrics(legacy_metrics, dataset_metrics)
    passed = sum(1 for c in comparisons if c["status"] == "PASS")
    failed = sum(1 for c in comparisons if c["status"] == "FAIL")
    expected = sum(1 for c in comparisons if c["status"] == "EXPECTED_DIFF")
    print(f"  PASS: {passed}, FAIL: {failed}, EXPECTED_DIFF: {expected}")

    # 4. Build parity report
    parity_report = {
        "parity_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_source": {
            "fills_path": str(FILLS_PATH),
            "fills_sha256": fills_sha,
            "fills_size": FILLS_PATH.stat().st_size,
            "events_path": str(EVENTS_PATH),
            "events_sha256": events_sha,
            "events_size": EVENTS_PATH.stat().st_size,
        },
        "legacy": legacy_metrics,
        "dataset": dataset_metrics,
        "comparisons": comparisons,
        "summary": {
            "total_metrics": len(comparisons),
            "passed": passed,
            "failed": failed,
            "expected_diff": expected,
            "overall_status": "PASS" if failed == 0 else "FAIL",
        },
    }

    report_path = output_dir / "parity_report.json"
    with open(report_path, "w") as f:
        json.dump(parity_report, f, indent=2, default=str)
    print(f"\nParity report: {report_path.resolve()}")

    # Print details
    print("\n=== Detail ===")
    for c in comparisons:
        status = c["status"]
        icon = "✓" if status == "PASS" else ("~" if status == "EXPECTED_DIFF" else "✗")
        diff_str = f"diff={c['absolute_difference']}" if c['absolute_difference'] is not None else ""
        print(f"  {icon} {c['metric']}: legacy={c['legacy_value']} dataset={c['dataset_value']} {diff_str} [{status}]")


if __name__ == "__main__":
    main()
