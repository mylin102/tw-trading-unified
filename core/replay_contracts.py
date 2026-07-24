"""
replay_contracts.py — Immutable replay case contracts and eligibility classification.

Phase 2A scope: RELEASE decision reproduction only.
ENTRY and EXIT decisions are explicitly out-of-scope for this phase.

Relationship to trade_dataset.py:
  - trade_dataset.py supplies data
  - replay_contracts.py defines replay contracts
  - replay engine (future) consumes normalized contracts

replay_case_id format: {dataset_content_hash}:{trade_id}:{decision_seq}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from core.trade_dataset import current_manifest, decision_level_view, load_dataset


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ReplayEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    MISSING_MARKET_CONTEXT = "MISSING_MARKET_CONTEXT"
    MISSING_LIFECYCLE_STATE = "MISSING_LIFECYCLE_STATE"
    MISSING_EFFECTIVE_PARAMETERS = "MISSING_EFFECTIVE_PARAMETERS"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    NON_DETERMINISTIC_INPUT = "NON_DETERMINISTIC_INPUT"
    VERSION_INCOMPATIBLE = "VERSION_INCOMPATIBLE"


class ReplayScope(str, Enum):
    """Phase 2A scope classification."""
    IN_SCOPE_RELEASE = "IN_SCOPE_RELEASE"                     # RELEASE_NEAR / RELEASE_FAR
    OUT_OF_SCOPE_ENTRY = "OUT_OF_SCOPE_ENTRY"                 # ENTRY — deferred to future phase
    OUT_OF_SCOPE_FINAL_EXIT = "OUT_OF_SCOPE_FINAL_EXIT"       # EXIT_NEAR / EXIT_FAR — deferred


class DecisionAction(str, Enum):
    """Stable enum for recorded actions, with LEGACY_REASON_MAP for vocabulary migration."""
    ENTRY = "ENTRY"
    RELEASE_NEAR = "RELEASE_NEAR"
    RELEASE_FAR = "RELEASE_FAR"
    EXIT_NEAR = "EXIT_NEAR"
    EXIT_FAR = "EXIT_FAR"

    @classmethod
    def from_decision_type(cls, dt: str) -> DecisionAction:
        return cls(dt)


# Legacy reason vocabulary migration
LEGACY_REASON_MAP: dict[str, str] = {
    "ATR_STOP": "ATR_DYNAMIC",
    "FIXED_STOP": "FIXED",
    "TMF_SPREAD_WIDE": "ENTRY_THRESHOLD",
    "TMF_SPREAD_NARROW": "ENTRY_THRESHOLD",
}


def normalize_reason(reason: str | None) -> str | None:
    """Map legacy reason vocabulary to current enum."""
    if reason is None:
        return None
    return LEGACY_REASON_MAP.get(reason, reason)


# ---------------------------------------------------------------------------
# Immutable replay case
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionReplayCase:
    """Immutable contract for replaying a single decision.
    All fields are PRE-decision state — never post-decision.
    """
    # Identity
    replay_case_id: str
    dataset_generation: str
    dataset_content_hash: str
    trade_id: str
    decision_seq: int
    decision_timestamp: str  # ISO format

    # Scope
    in_scope: bool                    # True only for RELEASE decisions in Phase 2A
    scope_label: str                  # IN_SCOPE_RELEASE | OUT_OF_SCOPE_ENTRY | OUT_OF_SCOPE_FINAL_EXIT

    # Recorded result
    recorded_action: str
    recorded_reason: str | None
    recorded_release_leg: str | None
    recorded_params_json: str | None  # Raw params from decision log

    # Pre-decision lifecycle state (from snapshot / facts)
    lifecycle_phase_before: str | None = None
    release_group_status_before: str | None = None
    near_leg_status_before: str | None = None
    far_leg_status_before: str | None = None
    remaining_leg_before: str | None = None

    # Market and position context
    near_price: float | None = None
    far_price: float | None = None
    spread: float | None = None
    z_score: float | None = None
    atr: float | None = None
    bb_position: str | None = None
    near_pnl: float | None = None
    far_pnl: float | None = None

    # Stateful guard inputs
    confirm_tick_count: int | None = None
    confirm_elapsed_ms: int | None = None
    action_elapsed_ms: int | None = None
    warmup_elapsed_ms: int | None = None
    warmup_tick_count: int | None = None

    # Effective configuration (from params_json or facts)
    direction: str | None = None                 # SELL_NEAR_BUY_FAR or BUY_NEAR_SELL_FAR
    near_side: str | None = None                 # SHORT or LONG (derived from direction)
    far_side: str | None = None                  # SHORT or LONG (derived from direction)
    release_stop_mode: str | None = None
    release_stop_threshold: float | None = None
    atr_multiplier: float | None = None
    confirm_ticks_required: int | None = None
    confirm_ms_required: int | None = None
    bb_filter_enabled: bool | None = None
    bb_bypass_multiplier: float | None = None

    # Eligibility
    eligibility_status: str = "PENDING"
    eligibility_reasons: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)

    # Provenance
    strategy_version: str | None = None
    schema_version: str = "0.2"
    source_event_type: str = "decision_log"

    # Timing invariant: snapshot must be pre-decision
    snapshot_timing: str | None = None  # PRE_DECISION | POST_DECISION

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict, excluding defaults for compact storage."""
        d = {}
        for k, v in self.__dict__.items():
            if v is None and k in ("lifecycle_phase_before", "release_group_status_before",
                                   "near_leg_status_before", "far_leg_status_before",
                                   "remaining_leg_before"):
                continue
            d[k] = v
        return d

    @property
    def normalized_reason(self) -> str | None:
        return normalize_reason(self.recorded_reason)


# ---------------------------------------------------------------------------
# Case builder
# ---------------------------------------------------------------------------


def _extract_params(params_json: str | None) -> dict:
    """Safely parse params_json from decision log."""
    if not params_json:
        return {}
    try:
        return json.loads(params_json) if isinstance(params_json, str) else params_json
    except (json.JSONDecodeError, TypeError):
        return {}


def _classify_scope(decision_type: str) -> tuple[bool, str]:
    """Classify a decision type into Phase 2A scope."""
    if decision_type in ("RELEASE_NEAR", "RELEASE_FAR"):
        return True, "IN_SCOPE_RELEASE"
    elif decision_type == "ENTRY":
        return False, "OUT_OF_SCOPE_ENTRY"
    elif decision_type in ("EXIT_NEAR", "EXIT_FAR"):
        return False, "OUT_OF_SCOPE_FINAL_EXIT"
    else:
        return False, f"OUT_OF_SCOPE_{decision_type}"


def _determine_release_stop_mode(params: dict, reason: str | None) -> str | None:
    """Extract release stop mode from params or reason."""
    risk_mode = params.get("risk_mode")
    if risk_mode:
        return risk_mode
    if reason:
        norm = normalize_reason(reason)
        if norm:
            return norm
    return None


def build_replay_cases(
    path: Optional[Path] = None,
    generation_id: Optional[str] = None,
    content_hash: Optional[str] = None,
) -> list[DecisionReplayCase]:
    """
    Build replay cases from the current published dataset.
    One case per decision. Idempotent: same generation → same cases.

    Returns a list of DecisionReplayCase, one per decision in the dataset.
    """
    manifest = current_manifest()
    gen = generation_id or manifest.get("dataset_build_id", "?")
    ch = content_hash or manifest.get("dataset_content_hash", "?")

    dv = decision_level_view(path)
    if dv.empty:
        return []

    ds = load_dataset(path)
    facts = ds.get("trade_facts", pd.DataFrame())

    cases: list[DecisionReplayCase] = []

    for _, row in dv.iterrows():
        trade_id = row.get("trade_id", "")
        decision_seq = int(row.get("decision_seq", 0))
        decision_type = row.get("decision_type", "?")
        dt_str = str(row.get("timestamp", ""))

        # Identity
        case_id = f"{ch}:{trade_id}:{decision_seq}"
        in_scope, scope_label = _classify_scope(decision_type)

        # Recorded result
        recorded_action = decision_type
        recorded_reason = row.get("reason")
        params_json = row.get("params_json")
        params = _extract_params(params_json)

        # Release leg: from recorded action type (RELEASE_NEAR → NEAR)
        recorded_release_leg = None
        if decision_type == "RELEASE_NEAR":
            recorded_release_leg = "NEAR"
        elif decision_type == "RELEASE_FAR":
            recorded_release_leg = "FAR"

        # Pre-decision state from snapshot
        z_score = row.get("z_score")
        atr = row.get("atr")
        spread = row.get("spread")
        near_price = row.get("price_near")
        far_price = row.get("price_far")
        bb_position = row.get("bb_position")

        # Pre-decision lifecycle state — NOT AVAILABLE from current dataset
        # These require lifecycle state capture which is a future improvement
        lifecycle_phase_before = None
        release_group_status_before = None
        near_leg_status_before = None
        far_leg_status_before = None
        remaining_leg_before = None

        # Near/far PnL from fact entry prices
        near_entry = None
        far_entry = None
        if not facts.empty and trade_id in facts["trade_id"].values:
            tf = facts[facts["trade_id"] == trade_id].iloc[0]
            near_entry = float(tf["near_entry_price"]) if pd.notna(tf.get("near_entry_price")) else None
            far_entry = float(tf["far_entry_price"]) if pd.notna(tf.get("far_entry_price")) else None

        # Direction and leg sides from facts merge
        direction = row.get("direction")
        near_side = "SHORT" if direction == "SELL_NEAR_BUY_FAR" else ("LONG" if direction == "BUY_NEAR_SELL_FAR" else None)
        far_side = "LONG" if direction == "SELL_NEAR_BUY_FAR" else ("SHORT" if direction == "BUY_NEAR_SELL_FAR" else None)

        # Effective parameters from params_json
        release_stop_mode = _determine_release_stop_mode(params, recorded_reason)
        release_stop_threshold = params.get("release_stop")
        atr_multiplier = params.get("stop_mult")

        # Snapshot timing
        snapshot_timing = "PRE_DECISION" if recorded_action in ("ENTRY", "RELEASE_NEAR", "RELEASE_FAR") else "UNKNOWN"

        # Build case
        case = DecisionReplayCase(
            replay_case_id=case_id,
            dataset_generation=gen,
            dataset_content_hash=ch,
            trade_id=trade_id,
            decision_seq=decision_seq,
            decision_timestamp=dt_str,
            in_scope=in_scope,
            scope_label=scope_label,
            recorded_action=recorded_action,
            recorded_reason=recorded_reason,
            recorded_release_leg=recorded_release_leg,
            recorded_params_json=params_json,
            lifecycle_phase_before=lifecycle_phase_before,
            release_group_status_before=release_group_status_before,
            near_leg_status_before=near_leg_status_before,
            far_leg_status_before=far_leg_status_before,
            remaining_leg_before=remaining_leg_before,
            near_price=near_price,
            far_price=far_price,
            spread=spread,
            z_score=z_score,
            atr=atr,
            bb_position=bb_position,
            near_pnl=near_entry,
            far_pnl=far_entry,
            direction=direction,
            near_side=near_side,
            far_side=far_side,
            release_stop_mode=release_stop_mode,
            release_stop_threshold=release_stop_threshold,
            atr_multiplier=atr_multiplier,
            snapshot_timing=snapshot_timing,
            strategy_version=None,
        )
        cases.append(case)

    return cases


# ---------------------------------------------------------------------------
# Eligibility classification
# ---------------------------------------------------------------------------

# Action-specific required fields for Phase 2A eligibility
RELEASE_REQUIRED_FIELDS: list[str] = [
    "atr",
    "release_stop_mode",
    "release_stop_threshold",
]

ENTRY_REQUIRED_FIELDS: list[str] = [
    "spread",
    "z_score",
]

EXIT_REQUIRED_FIELDS: list[str] = [
    "atr",
]


def _get_required_fields(decision_type: str) -> list[str]:
    """Return action-specific required fields for eligibility."""
    if decision_type in ("RELEASE_NEAR", "RELEASE_FAR"):
        return RELEASE_REQUIRED_FIELDS
    elif decision_type == "ENTRY":
        return ENTRY_REQUIRED_FIELDS
    elif decision_type in ("EXIT_NEAR", "EXIT_FAR"):
        return EXIT_REQUIRED_FIELDS
    return []


def classify_eligibility(cases: list[DecisionReplayCase]) -> list[DecisionReplayCase]:
    """
    Apply eligibility classification to all cases.
    Returns a new list with eligibility populated.
    """
    classified: list[DecisionReplayCase] = []
    for case in cases:
        reasons: list[str] = []
        missing: list[str] = []

        # 1. Scope check (out-of-scope cases are deferred, not failures)
        if not case.in_scope:
            if "ENTRY" in case.scope_label:
                reason = "Deferred to entry reproduction phase"
            elif "EXIT" in case.scope_label:
                reason = "Deferred to remaining-leg exit reproduction phase"
            else:
                reason = f"Unsupported action type: {case.recorded_action}"
            classified.append(DecisionReplayCase(
                **{k: v for k, v in case.__dict__.items()
                   if k not in ("eligibility_status", "eligibility_reasons", "missing_fields")},
                eligibility_status=ReplayEligibility.UNSUPPORTED_ACTION.value,
                eligibility_reasons=[reason],
                missing_fields=[],
            ))
            continue

        # 2. Required fields check (action-specific)
        required = _get_required_fields(case.recorded_action)
        for field in required:
            value = getattr(case, field, None)
            if value is None:
                missing.append(field)

        # 3. Effective parameters check
        if case.release_stop_mode is None:
            missing.append("release_stop_mode")
        if case.release_stop_threshold is None:
            missing.append("release_stop_threshold")
        if case.atr is None and case.recorded_action.startswith("RELEASE"):
            missing.append("atr")

        # 4. Lifecycle state — always missing from current dataset
        if case.lifecycle_phase_before is None:
            missing.append("lifecycle_phase_before")
        if case.release_group_status_before is None:
            missing.append("release_group_status_before")

        # Determine status
        if missing:
            non_lifecycle_missing = [
                m for m in missing
                if m not in ("lifecycle_phase_before", "release_group_status_before",
                             "near_leg_status_before", "far_leg_status_before",
                             "remaining_leg_before")
            ]
            if not non_lifecycle_missing:
                status = ReplayEligibility.ELIGIBLE.value
                reasons.append("Lifecycle state not captured")
            else:
                status = ReplayEligibility.MISSING_EFFECTIVE_PARAMETERS.value
                reasons.append(f"Missing: {', '.join(non_lifecycle_missing)}")
        else:
            status = ReplayEligibility.ELIGIBLE.value

        classified.append(DecisionReplayCase(
            **{k: v for k, v in case.__dict__.items()
               if k not in ("eligibility_status", "eligibility_reasons", "missing_fields")},
            eligibility_status=status,
            eligibility_reasons=reasons,
            missing_fields=missing,
        ))

    return classified


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def eligibility_report(cases: list[DecisionReplayCase]) -> dict[str, Any]:
    """Produce structured eligibility report."""
    total = len(cases)
    in_scope = [c for c in cases if c.in_scope]
    eligible = [c for c in cases if c.eligibility_status == ReplayEligibility.ELIGIBLE.value]
    ineligible = [c for c in cases if c.eligibility_status != ReplayEligibility.ELIGIBLE.value]

    status_counts: dict[str, int] = {}
    for c in cases:
        s = c.eligibility_status
        status_counts[s] = status_counts.get(s, 0) + 1

    scope_counts: dict[str, int] = {}
    for c in cases:
        s = c.scope_label
        scope_counts[s] = scope_counts.get(s, 0) + 1

    # Breakdown by ineligibility reason
    reason_breakdown: dict[str, int] = {}
    for c in ineligible:
        for r in c.eligibility_reasons:
            key = r[:80]
            reason_breakdown[key] = reason_breakdown.get(key, 0) + 1

    return {
        "total_decisions": total,
        "in_scope_release": len(in_scope),
        "eligible": len(eligible),
        "ineligible": len(ineligible),
        "eligibility_rate": round(len(eligible) / len(in_scope) * 100, 2) if in_scope else 0.0,
        "status_counts": status_counts,
        "scope_counts": scope_counts,
        "reason_breakdown": reason_breakdown,
        "eligible_release_count": len([c for c in eligible if c.recorded_action.startswith("RELEASE")]),
        "ineligible_release_count": len([c for c in in_scope if c.eligibility_status != ReplayEligibility.ELIGIBLE.value]),
        "missing_fields_summary": _missing_fields_summary(cases),
    }


def _missing_fields_summary(cases: list[DecisionReplayCase]) -> dict[str, int]:
    """Aggregate missing fields across all cases."""
    field_counts: dict[str, int] = {}
    for c in cases:
        for m in c.missing_fields:
            field_counts[m] = field_counts.get(m, 0) + 1
    return field_counts
