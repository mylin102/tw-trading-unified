# 2026-07-27 Gemini CLI: Policy J Cross Parameter Sweeper & Validity Audit Runner
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
                                "actual_final_net_pnl_twd": gross - 92.0,
                                "actual_mfe_net_pnl_twd": gross - 92.0,
                            }
            except Exception as e:
                print(f"Error reading {jsonl_file}: {e}", file=sys.stderr)

    return snaps, list(outcomes_dict.values())


def main():
    print("=" * 110)
    print("🔬 WAVE J2-C: POLICY J PARAMETER SWEEPER VALIDITY AUDIT RUNNER")
    print("=" * 110)

    base_dir = Path(__file__).resolve().parent.parent
    telemetry_dir = base_dir / "data" / "telemetry" / "shadow-soak"
    
    snaps, outcomes = load_telemetry_snapshots(telemetry_dir)
    print(f"📁 Loaded Telemetry Snapshots: {len(snaps)} snapshots for {len(outcomes)} trades from {telemetry_dir}")

    if not snaps:
        print("⚠️ No live telemetry trades found in local shadow soak folder. Injecting benchmark trajectory facts for Grid Search.")
        synth_data = [
            ("mts-synth-001", [
                (100.0, "10:00:00"), (400.0, "10:01:00"), (450.0, "10:02:00"),
                (380.0, "10:03:00"), (320.0, "10:04:00"), (260.0, "10:05:00"), (180.0, "10:06:00")
            ]),
            ("mts-synth-002", [
                (50.0, "11:00:00"), (200.0, "11:01:00"), (600.0, "11:02:00"),
                (520.0, "11:03:00"), (440.0, "11:04:00"), (360.0, "11:05:00")
            ]),
            ("mts-synth-003", [
                (100.0, "12:00:00"), (350.0, "12:01:00"), (310.0, "12:02:00"),
                (240.0, "12:03:00"), (180.0, "12:04:00")
            ]),
        ]
        snaps = []
        outcomes = []
        for tid, pnl_tuples in synth_data:
            pnls = [t[0] for t in pnl_tuples]
            max_pnl = max(pnls)
            last_pnl = pnls[-1]
            outcomes.append({
                "trade_id": tid,
                "actual_final_net_pnl_twd": last_pnl - 92.0,
                "actual_mfe_net_pnl_twd": max_pnl - 92.0,
            })
            for pnl, ts in pnl_tuples:
                snaps.append({
                    "trade_id": tid,
                    "eligibility_reason": "HEDGED_PAIR_SPREAD",
                    "gross_liquidation_pnl_twd": pnl,
                    "estimated_friction_twd": 0.0,
                    "near_quote_age_ms": 0,
                    "far_quote_age_ms": 0,
                    "event_time": f"2026-07-27T{ts}",
                })

    sweeper = PolicyJParameterSweeper(grid=ANCHOR_PARAMETER_GRID)
    cells, summaries = sweeper.sweep_landscape(snaps, outcomes)

    print("\n" + "=" * 110)
    print("📜 PER-TRADE x PARAMETER PAIR DETAILED AUDIT EVIDENCE (Dataset C)")
    print("=" * 110)
    print(f"{'Trade ID':<15} | {'Act/GB':<10} | {'Activated':<10} | {'Peak (TWD)':<12} | {'Trig Time':<10} | {'CF Net PnL':<12} | {'Act Net PnL':<12} | {'ΔNet PnL (TWD)'}")
    print("-" * 110)

    for cell in sorted(cells, key=lambda c: (c.trade_id, c.activation_twd, c.giveback_twd)):
        act_gb_str = f"{cell.activation_twd:.0f}/{cell.giveback_twd:.0f}"
        act_str = cell.activation_event_time[-8:] if cell.activation_event_time else "NEVER"
        trig_str = cell.trigger_event_time[-8:] if cell.trigger_event_time else "NONE"
        print(
            f"{cell.trade_id:<15} | "
            f"{act_gb_str:<10} | "
            f"{act_str:<10} | "
            f"{cell.peak_net_pnl_twd:<12.1f} | "
            f"{trig_str:<10} | "
            f"{cell.counterfactual_net_pnl_twd:<+12.1f} | "
            f"{cell.actual_net_pnl_twd:<+12.1f} | "
            f"{cell.delta_net_pnl_twd:<+14.2f}"
        )

    print("\n" + "=" * 125)
    print("📊 STATISTICAL AUDIT SUMMARY LANDSCAPE (Primary Endpoint: Mean ΔNet PnL per Source Trade)")
    print("=" * 125)
    print(f"{'Act/GB (TWD)':<12} | {'Src/Elig/Trig':<14} | {'Trig Rate':<10} | {'Primary: Mean Δ/Src':<20} | {'Mean Δ/Trig':<14} | {'Median Δ':<10} | {'Worst Δ':<10} | {'Win Rate'}")
    print("-" * 125)

    for summary in sorted(summaries, key=lambda s: s.mean_delta_per_source_trade, reverse=True):
        pair_str = f"{summary.activation_twd:.0f}/{summary.giveback_twd:.0f}"
        counts_str = f"{summary.source_trades}/{summary.eligible_trades}/{summary.triggered_trades}"
        print(
            f"{pair_str:<12} | "
            f"{counts_str:<14} | "
            f"{summary.trigger_rate:<10.2%} | "
            f"{summary.mean_delta_per_source_trade:<+20.2f} | "
            f"{summary.mean_delta_per_triggered_trade:<+14.2f} | "
            f"{summary.median_delta_net_pnl:<+10.2f} | "
            f"{summary.worst_delta_net_pnl:<+10.2f} | "
            f"{summary.win_rate:<10.2%}"
        )

    print("=" * 125)


if __name__ == "__main__":
    main()
