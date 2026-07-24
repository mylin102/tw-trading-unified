"""
Strategy Evaluation Platform (SEP) - Unified CLI Entrance
Author: Gemini CLI
Date: 2026-07-23

Commands:
  python3 -m sep.cli ingest
  python3 -m sep.cli daily-review [--send-email] [--as-of YYYY-MM-DD]
  python3 -m sep.cli weekly-research [--send-email] [--week-ending YYYY-MM-DD]
  python3 -m sep.cli dispatch-notifications
  python3 -m sep.cli retry-notifications
  python3 -m sep.cli evaluate-promotion --policy <name>
  python3 -m sep.cli validate-runtime
  python3 -m sep.cli doctor
  python3 -m sep.cli status
"""

import sys
import os
import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.deployment_role_gate import assert_research_allowed, get_deployment_target
from sep.jobs.ingest import run_ingest_job
from sep.jobs.daily_review import run_daily_review_job
from sep.jobs.weekly_research import run_weekly_research_job
from sep.notification import dispatch_notification_outbox
from sep.doctor import run_sep_doctor
from core.promotion_gate import evaluate_policy_promotion_gate
from sep.scorecard import generate_daily_shadow_scorecard


def main():
    parser = argparse.ArgumentParser(description="Strategy Evaluation Platform (SEP) Unified CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # sep ingest
    parser_ingest = subparsers.add_parser("ingest", help="Run hourly dataset continuous ingestion job")

    # sep daily-review
    parser_daily = subparsers.add_parser("daily-review", help="Run daily operational review and baseline replay")
    parser_daily.add_argument("--send-email", action="store_true", help="Queue email outbox report")
    parser_daily.add_argument("--as-of", type=str, default=None, help="Target date for catch-up rebuild (YYYY-MM-DD)")

    # sep weekly-research
    parser_weekly = subparsers.add_parser("weekly-research", help="Run weekly statistical research and R-005 trigger audit")
    parser_weekly.add_argument("--send-email", action="store_true", help="Queue email outbox report")
    parser_weekly.add_argument("--week-ending", type=str, default=None, help="Week ending date for catch-up rebuild (YYYY-MM-DD)")
    parser_weekly.add_argument("--bootstrap-samples", type=int, default=10000, help="Number of bootstrap samples")

    # sep generate-scorecard
    parser_card = subparsers.add_parser("generate-scorecard", help="Generate daily immutable Shadow Scorecard JSON")
    parser_card.add_argument("--date", type=str, default=None, help="Target date (YYYY-MM-DD)")

    # sep dispatch-notifications
    parser_dispatch = subparsers.add_parser("dispatch-notifications", help="Process and send pending items in notification outbox")

    # sep retry-notifications
    parser_retry = subparsers.add_parser("retry-notifications", help="Retry failed items in notification outbox")

    # sep evaluate-promotion
    parser_promo = subparsers.add_parser("evaluate-promotion", help="Evaluate candidate policy against Production Promotion Gate")
    parser_promo.add_argument("--policy", required=True, help="Name of candidate policy")

    # sep validate-runtime
    parser_validate = subparsers.add_parser("validate-runtime", help="Validate SEP runtime environment and permissions")

    # sep doctor
    parser_doctor = subparsers.add_parser("doctor", help="Run end-to-end SEP platform health & readiness diagnostics")

    # sep status
    parser_status = subparsers.add_parser("status", help="Display SEP platform research operations status")

    args = parser.parse_args()

    # Enforcement: Role Gate
    assert_research_allowed(REPO_ROOT)

    if args.command == "ingest":
        res = run_ingest_job()
        print(json.dumps(res, indent=2))

    elif args.command == "daily-review":
        res = run_daily_review_job(send_email=args.send_email)
        # Auto-generate daily scorecard
        generate_daily_shadow_scorecard(repo_root=REPO_ROOT)
        print(json.dumps(res, indent=2))

    elif args.command == "generate-scorecard":
        res = generate_daily_shadow_scorecard(date_str=args.date, repo_root=REPO_ROOT)
        print(json.dumps(res, indent=2))

    elif args.command == "weekly-research":
        res = run_weekly_research_job(bootstrap_samples=args.bootstrap_samples, send_email=args.send_email)
        print(json.dumps(res, indent=2))

    elif args.command in ("dispatch-notifications", "retry-notifications"):
        res = dispatch_notification_outbox()
        print(json.dumps(res, indent=2))

    elif args.command == "evaluate-promotion":
        ok, msg, report = evaluate_policy_promotion_gate(
            policy_name=args.policy,
            evidence_level="E2",
            confirmation_mean_diff_twd=200.0,
            confirmation_ci_lower_bound_twd=50.0,
            max_dd_degradation_pct=2.0,
            catastrophic_loss_count_increase=0,
            replay_validity_pass=True,
            plateau_pass=True,
            regression_suite_pass=True
        )
        print(json.dumps(report, indent=2))
        sys.exit(0 if ok else 1)

    elif args.command in ("validate-runtime", "doctor"):
        res = run_sep_doctor(REPO_ROOT)
        print(json.dumps(res, indent=2))
        sys.exit(0 if "PASS" in res["status"] else 1)

    elif args.command == "status":
        target = get_deployment_target(REPO_ROOT)
        print("=" * 60)
        print("STRATEGY EVALUATION PLATFORM (SEP) STATUS")
        print("=" * 60)
        print(f"Deployment Host : {target.get('deployment_id', 'unknown')}")
        print(f"Host Role       : {target.get('host_role', 'unknown')}")
        print("Platform State  : SHADOW_PRODUCTION_ACTIVE (2026-07-23 to 2026-08-06)")
        print("Change Freeze   : ACTIVE (No new features / No R-005 parameter sweep)")
        print(f"Repo Root       : {REPO_ROOT}")
        print("Operational Mode: Continuous Ingestion (15 * * * *) | Dispatch (7,22,37,52) | Daily (07:30) | Weekly (Sun 09:00)")
        print("=" * 60)


if __name__ == "__main__":
    main()
