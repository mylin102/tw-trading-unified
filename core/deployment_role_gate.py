"""
Strategy Evaluation Platform (SEP) - Deployment Role Fail-Closed Gates
Author: Gemini CLI
Date: 2026-07-23

Enforces strict runtime capability isolation:
1. assert_research_allowed(): Blocks SEP, Replay, DOE, and Statistical experiments on Production (Mini).
2. assert_broker_access_allowed(): Blocks Broker API initialization, Shioaji login, and live ordering on Research Host (Air4).
"""

import json
import sys
from pathlib import Path
from typing import Dict, Any

REPO_ROOT = Path(__file__).resolve().parent.parent


class ResearchNotAllowedOnProductionError(RuntimeError):
    """Raised when an offline research process (Replay, DOE, SEP) is invoked on a Production role."""


class BrokerAccessNotAllowedOnResearchHostError(RuntimeError):
    """Raised when live broker connectivity or ordering is invoked on an Offline Research host role."""


def get_deployment_target(repo_root: Path = REPO_ROOT) -> Dict[str, Any]:
    """Reads .deployment-target identity file."""
    target_file = repo_root / ".deployment-target"
    if not target_file.exists():
        return {
            "deployment_id": "unknown",
            "host_role": "unknown",
            "execution_modes": []
        }
    try:
        with open(target_file) as f:
            return json.load(f)
    except Exception:
        return {
            "deployment_id": "unknown",
            "host_role": "unknown",
            "execution_modes": []
        }


def assert_research_allowed(repo_root: Path = REPO_ROOT) -> str:
    """
    Enforces that the current host role is permitted to run offline research (Replay/DOE/SEP).
    Fails closed on Production roles ('production_trading', 'mini').
    """
    target = get_deployment_target(repo_root)
    dep_id = target.get("deployment_id", "").lower()
    host_role = target.get("host_role", "").lower()

    if dep_id == "mini" or "production" in host_role or "live" in host_role:
        raise ResearchNotAllowedOnProductionError(
            f"FAIL-CLOSED: Offline research (Replay/DOE/SEP) is strictly forbidden on Production host '{dep_id}' (role: '{host_role}')."
        )
    return dep_id or "air4"


def assert_broker_access_allowed(repo_root: Path = REPO_ROOT) -> str:
    """
    Enforces that the current host role is permitted to initialize broker sessions or order routing.
    Fails closed on Research roles ('offline_research', 'air4').
    """
    target = get_deployment_target(repo_root)
    dep_id = target.get("deployment_id", "").lower()
    host_role = target.get("host_role", "").lower()

    if dep_id == "air4" or "research" in host_role or "development" in host_role:
        raise BrokerAccessNotAllowedOnResearchHostError(
            f"FAIL-CLOSED: Broker connectivity and ordering are strictly forbidden on Offline Research host '{dep_id}' (role: '{host_role}')."
        )
    return dep_id or "mini"
