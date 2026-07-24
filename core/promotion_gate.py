"""
Strategy Evaluation Platform (SEP) - Multi-Factor Production Promotion Gate
Author: Gemini CLI
Date: 2026-07-23

Enforces multi-factor promotion criteria before any strategy policy candidate
can be promoted from Research (Air4) to Production (Mini):
1. Replay Validity PASS
2. Evidence Level >= E2
3. Confirmation Set Mean Improvement >= 150 TWD
4. Confirmation Set Bootstrap 95% CI Lower Bound >= 0 TWD
5. Max Drawdown Degradation <= 5%
6. Catastrophic Loss Count Increase <= 0
7. Parameter Robustness Plateau PASS
8. Production Regression Suite PASS
"""

import json
import sys
from pathlib import Path
from typing import Dict, Any, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent


class PolicyPromotionCriteriaFailedError(RuntimeError):
    """Raised when a candidate policy fails Production Promotion Gate checks."""


def evaluate_policy_promotion_gate(
    policy_name: str,
    evidence_level: str,
    confirmation_mean_diff_twd: float,
    confirmation_ci_lower_bound_twd: float,
    max_dd_degradation_pct: float,
    catastrophic_loss_count_increase: int,
    replay_validity_pass: bool,
    plateau_pass: bool,
    regression_suite_pass: bool,
    promotion_config: Dict[str, Any] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Evaluates whether a candidate policy satisfies all strict production promotion criteria.
    Returns: (promoted: bool, reason: str, report: dict)
    """
    if promotion_config is None:
        promotion_config = {
            "evidence_level_min": "E2",
            "confirmation_mean_improvement_twd_min": 150.0,
            "confirmation_ci_lower_bound_twd_min": 0.0,
            "max_drawdown_degradation_pct_max": 5.0,
            "catastrophic_loss_count_increase_max": 0,
            "replay_validity_required": True,
            "plateau_required": True,
            "regression_suite_pass_required": True
        }

    checks = {
        "replay_validity": (replay_validity_pass is True, f"Replay validity pass required: got {replay_validity_pass}"),
        "evidence_level": (evidence_level >= promotion_config["evidence_level_min"], f"Evidence level >= {promotion_config['evidence_level_min']}: got {evidence_level}"),
        "mean_improvement": (confirmation_mean_diff_twd >= promotion_config["confirmation_mean_improvement_twd_min"], f"Confirmation mean diff >= {promotion_config['confirmation_mean_improvement_twd_min']} TWD: got {confirmation_mean_diff_twd} TWD"),
        "ci_lower_bound": (confirmation_ci_lower_bound_twd >= promotion_config["confirmation_ci_lower_bound_twd_min"], f"Confirmation 95% CI lower bound >= {promotion_config['confirmation_ci_lower_bound_twd_min']} TWD: got {confirmation_ci_lower_bound_twd} TWD"),
        "max_drawdown": (max_dd_degradation_pct <= promotion_config["max_drawdown_degradation_pct_max"], f"Max DD degradation <= {promotion_config['max_drawdown_degradation_pct_max']}%: got {max_dd_degradation_pct}%"),
        "catastrophic_losses": (catastrophic_loss_count_increase <= promotion_config["catastrophic_loss_count_increase_max"], f"Catastrophic loss increase <= {promotion_config['catastrophic_loss_count_increase_max']}: got {catastrophic_loss_count_increase}"),
        "parameter_plateau": (plateau_pass is True, f"Parameter robustness plateau required: got {plateau_pass}"),
        "regression_suite": (regression_suite_pass is True, f"Regression suite pass required: got {regression_suite_pass}")
    }

    failed_reasons = [msg for ok, msg in checks.values() if not ok]

    report = {
        "policy_name": policy_name,
        "promoted": len(failed_reasons) == 0,
        "failed_reasons": failed_reasons,
        "checks": {k: ok for k, (ok, _) in checks.items()},
        "metrics": {
            "evidence_level": evidence_level,
            "confirmation_mean_diff_twd": confirmation_mean_diff_twd,
            "confirmation_ci_lower_bound_twd": confirmation_ci_lower_bound_twd,
            "max_dd_degradation_pct": max_dd_degradation_pct,
            "catastrophic_loss_count_increase": catastrophic_loss_count_increase,
            "replay_validity_pass": replay_validity_pass,
            "plateau_pass": plateau_pass,
            "regression_suite_pass": regression_suite_pass
        }
    }

    if failed_reasons:
        msg = f"PROMOTION REJECTED for '{policy_name}': " + "; ".join(failed_reasons)
        return False, msg, report

    return True, f"PROMOTION APPROVED for '{policy_name}' to Production PR Pipeline.", report
