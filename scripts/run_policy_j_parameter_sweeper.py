# 2026-07-27 Gemini CLI: Policy J Cross Parameter Sweeper Runner
import json
import sys
from pathlib import Path

from strategies.futures.mts.policy_j_parameter_sweeper import (
    ANCHOR_PARAMETER_GRID,
    PolicyJParameterSweeper,
)


def load_telemetry_snapshots(telemetry_dir: Path) -> tuple[list[dict], list[dict]]:
    """Load Policy J telemetry snapshots from shadow soak JSONL files."""
    snaps: list[dict] = []
    outcomes_dict: dict[str, dict] = {}

    if telemetry_dir.exists():
        for jsonl_file in telemetry_dir.glob("**/raw/*.jsonl"):
            try:
                with open(jsonl_file, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        rec = json.loads(line)
                        details = rec.get("details", {})
                        tid = details.get("trade_id") or rec.get("ticker", "UNKNOWN")
                        if tid:
                            details["trade_id"] = tid
                            snaps.append(details)
                            gross = details.get("gross_liquidation_pnl_twd", 0.0) or 0.0
                            outcomes_dict[tid] = {
                                "trade_id": tid,
                                "actual_net_pnl_twd": gross - 92.0,
                                "actual_mfe_net_pnl_twd": gross - 92.0,
                            }
            except Exception as e:
                print(f"Error reading {jsonl_file}: {e}", file=sys.stderr)

    return snaps, list(outcomes_dict.values())


def main():
    print("=" * 78)
    print("🔬 POLICY J PARAMETER GRID SEARCH & CROSS-BACKTEST RUNNER")
    print("=" * 78)

    base_dir = Path(__file__).resolve().parent.parent
    telemetry_dir = base_dir / "data" / "telemetry" / "shadow-soak"
    
    snaps, outcomes = load_telemetry_snapshots(telemetry_dir)
    print(f"📁 Loaded Telemetry Snapshots: {len(snaps)} snapshots for {len(outcomes)} trades from {telemetry_dir}")

    if not snaps:
        print("⚠️ No live telemetry trades found in local shadow soak folder. Injecting benchmark trajectory facts for Grid Search.")
        synth_data = [
            ("mts-synth-001", [100.0, 450.0, 250.0]),
            ("mts-synth-002", [200.0, 600.0, 400.0]),
            ("mts-synth-003", [150.0, 350.0, 220.0]),
            ("mts-synth-004", [50.0, 180.0, 100.0]),
            ("mts-synth-005", [300.0, 500.0, 320.0]),
            ("mts-synth-006", [120.0, 280.0, 190.0]),
        ]
        snaps = []
        outcomes = []
        for tid, pnl_series in synth_data:
            max_pnl = max(pnl_series)
            last_pnl = pnl_series[-1]
            outcomes.append({
                "trade_id": tid,
                "actual_net_pnl_twd": last_pnl - 92.0,
                "actual_mfe_net_pnl_twd": max_pnl - 92.0,
            })
            for i, pnl in enumerate(pnl_series):
                snaps.append({
                    "trade_id": tid,
                    "eligibility_reason": "HEDGED_PAIR_SPREAD",
                    "gross_liquidation_pnl_twd": pnl,
                    "estimated_friction_twd": 92.0,
                    "near_quote_age_ms": 0,
                    "far_quote_age_ms": 0,
                    "event_time": f"2026-07-27T10:0{i}:00",
                })

    sweeper = PolicyJParameterSweeper(grid=ANCHOR_PARAMETER_GRID)
    cells, summaries = sweeper.sweep_landscape(snaps, outcomes)

    print("\n" + "=" * 78)
    print(f"📊 GRID SEARCH LANDSCAPE RESULTS (11 Parameter Anchor Pairs)")
    print("=" * 78)
    print(f"{'Act (TWD)':<10} | {'GB (TWD)':<10} | {'Trig Rate':<10} | {'Mean Delta (TWD)':<18} | {'Win Rate':<10} | {'PED Imp (TWD)'}")
    print("-" * 78)

    best_pair = None
    best_mean_delta = -float("inf")

    for summary in sorted(summaries, key=lambda s: s.mean_delta_net_pnl, reverse=True):
        print(
            f"{summary.activation_twd:<10.1f} | "
            f"{summary.giveback_twd:<10.1f} | "
            f"{summary.trigger_rate:<10.2%} | "
            f"{summary.mean_delta_net_pnl:<+18.2f} | "
            f"{summary.win_rate:<10.2%} | "
            f"{summary.ped_improvement_total:<+12.2f}"
        )
        if summary.mean_delta_net_pnl > best_mean_delta:
            best_mean_delta = summary.mean_delta_net_pnl
            best_pair = summary

    print("=" * 78)
    if best_pair:
        print(
            f"🏆 OPTIMAL PARAMETER PAIR: activation_net_pnl_twd={best_pair.activation_twd:.1f} TWD, "
            f"giveback_twd={best_pair.giveback_twd:.1f} TWD\n"
            f"   (Mean Delta Improvement: {best_pair.mean_delta_net_pnl:+.2f} TWD, Win Rate: {best_pair.win_rate:.2%}, Total PED Improvement: {best_pair.ped_improvement_total:+.2f} TWD)"
        )


if __name__ == "__main__":
    main()
