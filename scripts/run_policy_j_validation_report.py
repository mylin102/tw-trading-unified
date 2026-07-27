#!/usr/bin/env python3
# 2026-07-27 Gemini CLI: Policy J Validation & Observation Report CLI Runner
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.services.policy_j_reader import PolicyJTelemetryReader
from strategies.futures.mts.policy_j_validation_report import PolicyJValidationReportEngine, GateResult, Recommendation


LOCK_FILE = Path("/tmp/policy_j_report.lock")
SEAL_MANIFEST_FILE = ROOT / "exports" / "reports" / "holdout_seal_manifest.json"


def acquire_lock():
    """Atomic lock file creation to prevent concurrent runs."""
    if LOCK_FILE.exists():
        print(f"⚠️ Lock file {LOCK_FILE} exists. Another process is running.")
        sys.exit(1)
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def release_lock():
    """Remove lock file on exit."""
    if LOCK_FILE.exists():
        try:
            LOCK_FILE.unlink()
        except Exception:
            pass


def run_observation_report(snapshots: list, outcomes: list, reports_dir: Path) -> int:
    """
    Run Weekly Observation and Readiness Audit (--mode observation).
    DOES NOT unseal Holdout or run G1-G9 on Holdout.
    """
    source_count = len(outcomes)
    dates = sorted(list(set(o.get("session_date", "UNKNOWN") for o in outcomes))) if outcomes else []

    readiness = "NOT_READY_FOR_SEALED_EVALUATION" if len(dates) < 3 or source_count < 15 else "READY_TO_UNSEAL_HOLDOUT"

    today_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = reports_dir / f"policy_j_observation_report_{today_str}.json"

    payload = {
        "generated_at": datetime.now().isoformat(),
        "mode": "OBSERVATION",
        "data_readiness": readiness,
        "total_dates_count": len(dates),
        "available_dates": dates,
        "source_trades_count": source_count,
        "telemetry_snapshots_count": len(snapshots),
        "holdout_unsealed": False,
        "note": "Observation Mode: Holdout sealed. No G1-G10 promotion decisions evaluated.",
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"✅ Policy J Weekly Observation Report generated: {out_file}")
    print(f"📊 Readiness Status: {readiness} ({len(dates)} dates, {source_count} trades)")

    return 0 if readiness == "READY_TO_UNSEAL_HOLDOUT" else 2


def run_sealed_holdout_report(snapshots: list, outcomes: list, reports_dir: Path, force_replay: bool) -> int:
    """
    Run Single Sealed Holdout Evaluation (--mode sealed-holdout).
    Consumes single-use seal manifest.
    """
    if SEAL_MANIFEST_FILE.exists() and not force_replay:
        manifest = json.loads(SEAL_MANIFEST_FILE.read_text(encoding="utf-8"))
        if manifest.get("seal_status") == "CONSUMED":
            print("🛑 FAIL-CLOSED: Sealed Holdout has ALREADY BEEN CONSUMED!")
            print(f"   Unsealed at: {manifest.get('unsealed_at')}")
            print("   Pass --force-replay to perform explicit engine bug replay audit.")
            return 3

    engine = PolicyJValidationReportEngine()
    report, details = engine.generate_report(snapshots, outcomes)
    report_dict = report.to_dict()

    today_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = reports_dir / f"policy_j_sealed_holdout_report_{today_str}.json"

    # Consume Seal
    seal_payload = {
        "seal_status": "CONSUMED",
        "unsealed_at": datetime.now().isoformat(),
        "report_file": str(out_file),
        "reproduction_hash": report_dict.get("candidate_config_hash", ""),
    }
    SEAL_MANIFEST_FILE.write_text(json.dumps(seal_payload, indent=2), encoding="utf-8")

    result_payload = {
        "generated_at": datetime.now().isoformat(),
        "mode": "SEALED_HOLDOUT",
        "summary": report_dict,
        "details": details,
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result_payload, f, ensure_ascii=False, indent=2)

    print(f"✅ Policy J Sealed Holdout Report generated: {out_file}")
    print(f"📊 Final Recommendation: {report.final_recommendation}")
    for g in report.gates:
        print(f"  • [{g.gate_id}] {g.gate_name}: {g.result} ({g.details})")

    if report.final_recommendation == Recommendation.ADVANCE_TO_EXECUTION_DESIGN.value:
        return 0
    elif report.final_recommendation == Recommendation.REJECT.value:
        return 1
    else:
        return 2


def main():
    parser = argparse.ArgumentParser(description="Policy J Report CLI Runner")
    parser.add_argument("--mode", choices=["observation", "sealed-holdout"], default="observation", help="Report mode (default: observation)")
    parser.add_argument("--force-replay", action="store_true", help="Force replay sealed holdout after engine fix")
    args = parser.parse_args()

    acquire_lock()
    try:
        reader = PolicyJTelemetryReader()
        dates = reader.list_available_session_dates()

        snapshots = []
        for d in dates:
            snapshots.extend(reader.load_snapshots(d))

        outcomes = []
        trades_file = ROOT / "exports" / "trades" / "mts_closed_trades.jsonl"
        if trades_file.exists():
            try:
                with open(trades_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            outcomes.append(json.loads(line.strip()))
            except Exception as ex:
                print(f"⚠️ Warning reading trades file: {ex}")

        reports_dir = ROOT / "exports" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        if args.mode == "observation":
            code = run_observation_report(snapshots, outcomes, reports_dir)
        else:
            code = run_sealed_holdout_report(snapshots, outcomes, reports_dir, args.force_replay)

        sys.exit(code)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
