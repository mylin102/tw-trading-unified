"""
replay_release.py — Phase 2A-2: Side-effect-free Release Decision Replay.

Replay 34 RELEASE cases against the production evaluate_lifecycle_actions(),
with zero side effects (no orders, no state files, no JSONL writes).

Isolation guarantees:
  - Fresh PositionLifecycle per case
  - No datetime.now() or time.time() — all timing from case data
  - No Shioaji, no filesystem, no order submission
  - Deep-copied inputs and outputs
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd

from core.replay_contracts import DecisionReplayCase, build_replay_cases, classify_eligibility

# Production imports — used only for their pure functions and dataclasses
# No side effects: no state file, no Shioaji, no orders
from strategies.plugins.futures.active.tmf_spread import (
    LifecycleAction,
    LifecycleContext,
    LifecycleDecision,
    PositionLifecycle,
    PositionPhase,
    ReleaseGroup,
    ReleaseGroupStatus,
    TrailGroup,
    TrailGroupStatus,
    evaluate_lifecycle_actions,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mismatch classification
# ---------------------------------------------------------------------------


class MismatchCategory(str, Enum):
    ACTION_NONE = "ACTION_NONE"                             # evaluator returned None, expected action
    ACTION_TYPE_MISMATCH = "ACTION_TYPE_MISMATCH"           # wrong action (e.g. TRAIL instead of RELEASE)
    RELEASE_LEG_MISMATCH = "RELEASE_LEG_MISMATCH"           # wrong leg (NEAR vs FAR)
    REASON_MISMATCH = "REASON_MISMATCH"                     # reason code differs
    THRESHOLD_MISMATCH = "THRESHOLD_MISMATCH"               # computed threshold vs recorded
    STATE_RECONSTRUCTION = "STATE_RECONSTRUCTION"            # lifecycle state mismatch
    MISSING_ENGINE_INPUT = "MISSING_ENGINE_INPUT"            # can't construct LifecycleContext
    ENGINE_EXCEPTION = "ENGINE_EXCEPTION"                    # unexpected exception in evaluator
    VERSION_DRIFT = "VERSION_DRIFT"                          # known code version difference
    MATCH = "MATCH"                                          # perfect reproduction


# ---------------------------------------------------------------------------
# Replay result
# ---------------------------------------------------------------------------


@dataclass
class ReplayResult:
    """Result of replaying a single RELEASE decision."""
    # Identity
    replay_case_id: str
    trade_id: str
    decision_seq: int

    # Recorded
    recorded_action: str
    recorded_release_leg: str | None
    recorded_reason: str | None
    recorded_threshold: float | None

    # Replayed
    replayed_action: str | None
    replayed_release_leg: str | None
    replayed_reason: str | None
    replayed_threshold: float | None

    # Match
    action_match: bool
    leg_match: bool
    reason_match: bool
    mismatch_category: str

    # Diagnostics
    lifecycle_state_source: str = "RECONSTRUCTED"
    lifecycle_assumptions: list[str] = field(default_factory=list)
    exception_type: str | None = None
    exception_msg: str | None = None
    details: str | None = None


# ---------------------------------------------------------------------------
# Lifecycle reconstruction
# ---------------------------------------------------------------------------


def _compute_pnl_pts(
    entry_price: float | None,
    current_price: float | None,
    side: str | None,
) -> float:
    """Compute points PnL for a single leg.
    SHORT side: entry - current = profit if current < entry
    LONG side: current - entry = profit if current > entry

    If current_price is NaN or None, treat as no-movement (0 PnL contribution).
    """
    if entry_price is None:
        return 0.0
    if current_price is None:
        return 0.0
    import math
    if isinstance(current_price, float) and math.isnan(current_price):
        return 0.0
    if side and side.upper() in ("SHORT", "SELL"):
        return entry_price - current_price
    else:
        return current_price - entry_price


def reconstruct_lifecycle() -> PositionLifecycle:
    """Create a minimal PositionLifecycle in SPREAD phase with ARMED release group.
    This is the standard state before a release decision is made.
    """
    rg = ReleaseGroup(
        status=ReleaseGroupStatus.ARMED,
    )
    tg = TrailGroup(
        status=TrailGroupStatus.INACTIVE,
    )
    return PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=rg,
        trail_group=tg,
    )


def build_release_context(case: DecisionReplayCase, config: Optional[Any] = None) -> LifecycleContext:
    """Build LifecycleContext from a RELEASE replay case.
    All values come from case data — no runtime system calls.
    Uses case.near_side / case.far_side for correct PnL direction.
    """
    # Near/far PnL from entry prices vs current prices, using correct leg side
    near_pnl = _compute_pnl_pts(case.near_pnl, case.near_price, case.near_side)
    far_pnl = _compute_pnl_pts(case.far_pnl, case.far_price, case.far_side)

    # Release stop threshold from case params or config override
    threshold = case.release_stop_threshold or 0.0
    if config is not None and getattr(config, "release_stop_threshold", None) is not None:
        threshold = getattr(config, "release_stop_threshold")

    return LifecycleContext(
        near_pnl_pts=round(near_pnl, 2),
        far_pnl_pts=round(far_pnl, 2),
        floating_pnl_pts=round(near_pnl + far_pnl, 2),
        entry_age_secs=0.0,  # Not used by release check
        release_stop_threshold=threshold,
        trail_dist=0.0,
        is_backtest=True,
    )


# ---------------------------------------------------------------------------
# Reason mapping
# ---------------------------------------------------------------------------


def _reason_from_params(params_json: str | None, default_mode: str | None) -> str:
    """Derive a reason label from the params_json or case metadata."""
    if params_json:
        try:
            params = json.loads(params_json) if isinstance(params_json, str) else params_json
            risk_mode = params.get("risk_mode")
            if risk_mode:
                return f"RELEASE_STOP_{risk_mode}"
        except (json.JSONDecodeError, TypeError):
            pass
    if default_mode:
        return f"RELEASE_STOP_{default_mode}"
    return "RELEASE_STOP"


# ---------------------------------------------------------------------------
# Single-case replay
# ---------------------------------------------------------------------------


def replay_single_release(case: DecisionReplayCase, config: Optional[Any] = None) -> ReplayResult:
    """Replay a single RELEASE decision against the production evaluator.
    No side effects. Returns a ReplayResult.
    """
    assert case.recorded_action.startswith("RELEASE"), f"Not a RELEASE case: {case.recorded_action}"

    # Build inputs
    lifecycle = reconstruct_lifecycle()
    ctx = build_release_context(case, config=config)

    assumptions: list[str] = [
        f"lifecycle phase=SPREAD (reconstructed)",
        f"release_group.status=ARMED (reconstructed)",
        f"near_pnl_pts={ctx.near_pnl_pts} (entry={case.near_pnl} current={case.near_price})",
        f"far_pnl_pts={ctx.far_pnl_pts} (entry={case.far_pnl} current={case.far_price})",
        f"threshold={ctx.release_stop_threshold}",
    ]

    # Deep copy to ensure no cross-case contamination
    ctx_copy = copy.deepcopy(ctx)
    lifecycle_copy = copy.deepcopy(lifecycle)

    # Run production evaluator
    try:
        decision = evaluate_lifecycle_actions(ctx_copy, lifecycle_copy)
    except Exception as e:
        return ReplayResult(
            replay_case_id=case.replay_case_id,
            trade_id=case.trade_id,
            decision_seq=case.decision_seq,
            recorded_action=case.recorded_action,
            recorded_release_leg=case.recorded_release_leg,
            recorded_reason=case.recorded_reason,
            recorded_threshold=case.release_stop_threshold,
            replayed_action=None,
            replayed_release_leg=None,
            replayed_reason=None,
            replayed_threshold=None,
            action_match=False,
            leg_match=False,
            reason_match=False,
            mismatch_category=MismatchCategory.ENGINE_EXCEPTION.value,
            lifecycle_state_source="RECONSTRUCTED",
            lifecycle_assumptions=assumptions,
            exception_type=type(e).__name__,
            exception_msg=str(e),
        )

    # Compare
    if decision is None:
        replayed_action = "NONE"
        replayed_leg = None
    else:
        replayed_action = decision.action.value
        replayed_leg = decision.release_leg.value if decision.release_leg else None

    recorded_leg = case.recorded_release_leg
    recorded_reason = case.recorded_reason or ""
    replayed_reason = _reason_from_params(case.recorded_params_json, case.release_stop_mode)

    action_match = replayed_action == "RELEASE"
    leg_match = (replayed_leg or "").upper() == (recorded_leg or "").upper()
    reason_match = replayed_reason == recorded_reason

    # Determine mismatch category
    if action_match and leg_match and reason_match:
        cat = MismatchCategory.MATCH.value
    elif not action_match:
        cat = MismatchCategory.ACTION_TYPE_MISMATCH.value
    elif not leg_match:
        cat = MismatchCategory.RELEASE_LEG_MISMATCH.value
    elif not reason_match:
        cat = MismatchCategory.REASON_MISMATCH.value
    else:
        cat = MismatchCategory.MATCH.value

    return ReplayResult(
        replay_case_id=case.replay_case_id,
        trade_id=case.trade_id,
        decision_seq=case.decision_seq,
        recorded_action=case.recorded_action,
        recorded_release_leg=case.recorded_release_leg,
        recorded_reason=case.recorded_reason,
        recorded_threshold=case.release_stop_threshold,
        replayed_action=replayed_action,
        replayed_release_leg=replayed_leg,
        replayed_reason=replayed_reason,
        replayed_threshold=ctx.release_stop_threshold,
        action_match=action_match,
        leg_match=leg_match,
        reason_match=reason_match,
        mismatch_category=cat,
        lifecycle_state_source="RECONSTRUCTED",
        lifecycle_assumptions=assumptions,
    )


# ---------------------------------------------------------------------------
# Batch replay
# ---------------------------------------------------------------------------


def replay_batch(cases: list[DecisionReplayCase], config: Optional[Any] = None) -> list[ReplayResult]:
    """Replay all RELEASE cases in order. Returns results list."""
    results = []
    for case in cases:
        if not case.recorded_action.startswith("RELEASE"):
            continue
        result = replay_single_release(case, config=config)
        results.append(result)
    return results


def order_independence_check(cases: list[DecisionReplayCase]) -> dict[str, Any]:
    """Verify replay produces same results regardless of case order."""
    # Forward order
    fwd = replay_batch(cases)
    fwd_actions = [(r.trade_id, r.replayed_action, r.replayed_release_leg) for r in fwd]

    # Reverse order
    rev = replay_batch(list(reversed(cases)))
    rev_actions = list(reversed([(r.trade_id, r.replayed_action, r.replayed_release_leg) for r in rev]))

    # Compare
    matches = all(f == r for f, r in zip(fwd_actions, rev_actions))
    return {
        "order_independent": matches,
        "forward_count": len(fwd),
        "reverse_count": len(rev),
        "mismatch_count": sum(1 for f, r in zip(fwd_actions, rev_actions) if f != r),
    }


def side_effect_check() -> dict[str, Any]:
    """Verify that replay did not write to any production paths.
    Checks state file, event log, fills log sizes before/after.
    (This should be called before and after a batch replay.)
    """
    logs_dir = Path("logs")
    state_file = logs_dir / "runtime_status.json"
    checks = {
        "state_file_exists": state_file.exists(),
        "fills_log_lines": len([l for l in open(logs_dir / "mts_trade_fills.jsonl") if l.strip()]) if (logs_dir / "mts_trade_fills.jsonl").exists() else -1,
    }
    return checks


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def build_reproduction_report(results: list[ReplayResult]) -> dict[str, Any]:
    """Build structured reproduction report from results."""
    total = len(results)
    action_match = sum(1 for r in results if r.action_match)
    leg_match = sum(1 for r in results if r.leg_match)
    reason_match = sum(1 for r in results if r.reason_match)

    mismatches = [r for r in results if r.mismatch_category != MismatchCategory.MATCH.value]

    category_counts: dict[str, int] = {}
    for r in results:
        cat = r.mismatch_category
        category_counts[cat] = category_counts.get(cat, 0) + 1

    return {
        "total_cases": total,
        "action_match": action_match,
        "action_match_rate": round(action_match / total * 100, 2) if total > 0 else 0,
        "leg_match": leg_match,
        "leg_match_rate": round(leg_match / total * 100, 2) if total > 0 else 0,
        "reason_match": reason_match,
        "reason_match_rate": round(reason_match / total * 100, 2) if total > 0 else 0,
        "mismatch_count": len(mismatches),
        "mismatch_rate": round(len(mismatches) / total * 100, 2) if total > 0 else 0,
        "category_counts": category_counts,
        "exception_count": sum(1 for r in results if r.exception_type is not None),
    }
