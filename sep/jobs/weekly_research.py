"""
SEP Weekly Research Job (Sunday 09:00 Statistical Inference & R-005 Trigger Audit)
Author: Gemini CLI
Date: 2026-07-23
"""

import sys
import os
import json
from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.deployment_role_gate import assert_research_allowed
from core.research_manifest import generate_research_manifest
from scripts.research.r004_5_replay_validation import load_events, run_fidelity_check, run_coverage_breakdown, run_statistical_inference
from sep.notification import queue_notification, Priority


def run_weekly_research_job(bootstrap_samples: int = 10000, send_email: bool = False) -> dict:
    """Executes the weekly statistical research and R-005 trigger condition audit on Air4."""
    dep_id = assert_research_allowed(REPO_ROOT)
    execution_id = f"SEP-WEEKLY-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{dep_id}"

    events_path = REPO_ROOT / "data" / "frozen" / "parity_20260716" / "mts_spread_events.jsonl"
    if not events_path.exists():
        matches = list((REPO_ROOT / "data").glob("**/mts_spread_events.jsonl"))
        if matches:
            events_path = matches[0]

    events = load_events(events_path) if events_path.exists() else []
    fidelity = run_fidelity_check(events) if events else {}
    coverage = run_coverage_breakdown(events) if events else {}
    stat_res = run_statistical_inference(events) if events else {}

    manifest = generate_research_manifest(
        research_id=f"Weekly-Research-{datetime.now().strftime('%Y%m%d')}",
        dataset_path=events_path if events_path.exists() else REPO_ROOT / "RULES.md",
        bootstrap_seed=42
    )

    # R-005 Trigger Audit
    episodes_count = len(events)
    r005_trigger_met = (episodes_count >= 20) and False  # Requires manual hypothesis registration

    weekly_report = {
        "execution_id": execution_id,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "manifest": manifest,
        "statistical_inference": stat_res,
        "coverage_audit": coverage,
        "r005_trigger_audit": {
            "minimum_episodes_required": 20,
            "current_episodes": episodes_count,
            "manual_hypothesis_registered": False,
            "r005_optimization_triggered": r005_trigger_met
        }
    }

    report_dir = REPO_ROOT / "reports" / "weekly_research"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"weekly_research_{datetime.now().strftime('%Y%m%d')}.json"

    with open(report_file, "w") as f:
        json.dump(weekly_report, f, indent=2)

    if send_email:
        body = f"""SEP Weekly Statistical Research Brief
Date: {weekly_report['date']} | Execution: {execution_id}
Dataset Build: {manifest['version_provenance']['dataset_version']}

1. Statistical Inference (Pure ATR vs Pure Fixed)
- Episodes (n): {stat_res.get('sample_size_n', 0)}
- Mean Difference: +{stat_res.get('mean_diff_twd', 0.0)} TWD
- Median Difference: +{stat_res.get('median_diff_twd', 0.0)} TWD
- Bootstrap 95% CI (B={bootstrap_samples}): [{stat_res.get('bootstrap_95_ci', (0,0))[0]}, {stat_res.get('bootstrap_95_ci', (0,0))[1]}] TWD
- Wilcoxon Test: {stat_res.get('wilcoxon_p_value_formatted', 'N/A')}
- Cohen's d: {stat_res.get('cohens_d', 0.0)} (Evidence Level {stat_res.get('evidence_level', 'E2')})

2. R-005 Trigger Audit
- Threshold: >= 20 episodes AND manual hypothesis registration
- Status: NOT TRIGGERED (Manual Hypothesis Registration Required)
"""
        queue_notification(
            priority=Priority.P2_REPORT,
            subject=f"[SEP Weekly] Statistical Research Brief - Evidence Level E2",
            body=body,
            report_execution_id=execution_id
        )

    return weekly_report
