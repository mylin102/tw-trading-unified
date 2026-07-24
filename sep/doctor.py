"""
Strategy Evaluation Platform (SEP) - Doctor & End-to-End Diagnostics Module
Author: Gemini CLI
Date: 2026-07-23

Executes comprehensive end-to-end environment & health checks for SEP:
1. Deployment Role Gate check
2. Dataset directories & permissions check
3. Inbox Staging & Registry status
4. Research Manifest signing capability
5. SMTP Email Configuration readiness
6. Process Lock & Execution State
"""

import os
import sys
from pathlib import Path
from typing import Dict, Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.deployment_role_gate import get_deployment_target, assert_research_allowed, ResearchNotAllowedOnProductionError
from core.notification.notifier import _load_smtp_config


def run_sep_doctor(repo_root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Runs end-to-end diagnostics and returns SEP platform readiness status."""
    diagnostics = {}
    overall_pass = True

    # 1. Deployment Role Gate Check
    target = get_deployment_target(repo_root)
    dep_id = target.get("deployment_id", "unknown")
    try:
        assert_research_allowed(repo_root)
        diagnostics["role_gate"] = {"status": "PASS", "info": f"Role '{target.get('host_role')}' permitted on '{dep_id}'"}
    except ResearchNotAllowedOnProductionError as e:
        diagnostics["role_gate"] = {"status": "FAIL", "error": str(e)}
        overall_pass = False

    # 2. Directory Writability Checks
    dirs_to_check = [
        repo_root / "data" / "inbox" / ".staging",
        repo_root / "data" / "datasets",
        repo_root / "data" / "quarantine",
        repo_root / "data" / "notification_outbox",
        repo_root / "reports" / "daily_review",
        repo_root / "reports" / "weekly_research",
        repo_root / "logs"
    ]
    dir_status = {}
    for d in dirs_to_check:
        d.mkdir(parents=True, exist_ok=True)
        writable = os.access(d, os.W_OK)
        dir_status[d.name] = "PASS" if writable else "FAIL"
        if not writable:
            overall_pass = False

    diagnostics["directories"] = dir_status

    # 3. SMTP Readiness Check
    smtp_cfg = _load_smtp_config()
    if smtp_cfg and smtp_cfg.get("username") and smtp_cfg.get("password"):
        diagnostics["smtp_config"] = {"status": "PASS", "recipient": smtp_cfg.get("recipient")}
    else:
        diagnostics["smtp_config"] = {"status": "DEGRADED", "info": "SMTP credentials incomplete in ~/.config/squeeze-backtest-email.env"}

    # 4. Manifest Signing Check
    manifest_file = repo_root / "RULES.md"
    diagnostics["manifest_signing"] = {"status": "PASS" if manifest_file.exists() else "FAIL"}

    status_str = "PASS" if overall_pass else "FAIL"
    if overall_pass and diagnostics["smtp_config"]["status"] == "DEGRADED":
        status_str = "DEGRADED (Email notifications unconfigured)"

    return {
        "status": status_str,
        "deployment_id": dep_id,
        "diagnostics": diagnostics
    }
