#!/usr/bin/env python3
# 2026-07-27 Gemini CLI: Policy J Validation Report CLI Runner for Cron & Automated Audits
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.services.policy_j_reader import PolicyJTelemetryReader
from strategies.futures.mts.policy_j_validation_report import PolicyJValidationReportEngine


def run_audit_report() -> dict:
    """Run Policy J Validation Report and save to exports/reports/."""
    reader = PolicyJTelemetryReader()
    dates = reader.list_available_session_dates()

    snapshots = []
    for d in dates:
        snapshots.extend(reader.load_snapshots(d))

    # Read actual trade outcomes from exports/trades/ if available
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

    engine = PolicyJValidationReportEngine()
    report, details = engine.generate_report(snapshots, outcomes)
    report_dict = report.to_dict()

    # Save report to exports/reports/
    reports_dir = ROOT / "exports" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = reports_dir / f"policy_j_validation_report_{today_str}.json"

    result_payload = {
        "generated_at": datetime.now().isoformat(),
        "summary": report_dict,
        "details": details,
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result_payload, f, ensure_ascii=False, indent=2)

    print(f"✅ Policy J Validation Report successfully generated: {out_file}")
    print(f"📊 Final Recommendation: {report.final_recommendation}")
    for g in report.gates:
        print(f"  • [{g.gate_id}] {g.gate_name}: {g.result} ({g.details})")

    return result_payload


if __name__ == "__main__":
    run_audit_report()
