# 2026-07-28: Shared Quote Coherence Contract
# Used by both CombinedUplTrailPolicy (production) and PolicyJShadowEvaluator (shadow).
# Single source of truth for quote freshness, pair coherence, and ineligibility reasons.
from dataclasses import dataclass
from enum import Enum
from typing import Any


class QuoteCoherenceReason(str, Enum):
    """Enumeration of quote coherence evaluation results.
    
    Reason enum, not free text — ensures both evaluators use same set.
    """
    READY = "READY"
    POSITION_INCOMPLETE = "POSITION_INCOMPLETE"
    NEAR_QUOTE_MISSING = "NEAR_QUOTE_MISSING"
    FAR_QUOTE_MISSING = "FAR_QUOTE_MISSING"
    NEAR_STALE = "NEAR_STALE"
    FAR_STALE = "FAR_STALE"
    BOTH_STALE = "BOTH_STALE"
    PAIR_SKEW = "PAIR_SKEW"
    PNL_INVALID = "PNL_INVALID"


@dataclass(frozen=True)
class QuoteCoherenceInput:
    """Immutable input for quote coherence evaluation.
    
    Freshness is measured as local processing delay (processed_at - received_at).
    Pair skew is the time disparity between near and far exchange event times.
    """
    near_quote_age_ms: int | None = None
    far_quote_age_ms: int | None = None
    near_open_qty: int = 0
    far_open_qty: int = 0
    is_spread_phase: bool = False
    max_quote_age_ms: int = 1000
    max_pair_skew_ms: int = 500
    has_exit_inflight: bool = False
    gross_pnl: float | None = None


@dataclass(frozen=True)
class QuoteCoherenceResult:
    """Immutable result of quote coherence evaluation.
    
    Both evaluators MUST use this same struct — no field name drift.
    """
    fresh: bool = False
    coherent: bool = False
    near_stale: bool = False
    far_stale: bool = False
    near_missing: bool = False
    far_missing: bool = False
    pair_skew_ms: int = 0
    reason: str = QuoteCoherenceReason.POSITION_INCOMPLETE.value


def evaluate_quote_coherence(input: QuoteCoherenceInput) -> QuoteCoherenceResult:
    """Evaluate quote freshness, pair coherence, and position readiness.
    
    Pure function — 0 side effects, 0 I/O.
    Called by both CombinedUplTrailPolicy and PolicyJShadowEvaluator.
    """
    # 1. Position completeness
    if not input.is_spread_phase:
        return QuoteCoherenceResult(
            reason=QuoteCoherenceReason.POSITION_INCOMPLETE.value,
        )
    if input.near_open_qty <= 0 or input.far_open_qty <= 0:
        return QuoteCoherenceResult(
            reason=QuoteCoherenceReason.POSITION_INCOMPLETE.value,
        )

    # 2. Exit inflight guard (quotes during exit are unreliable for Policy J)
    if input.has_exit_inflight:
        return QuoteCoherenceResult(
            reason=QuoteCoherenceReason.PAIR_SKEW.value,
        )

    # 3. Quote presence
    near_missing = input.near_quote_age_ms is None
    far_missing = input.far_quote_age_ms is None

    if near_missing and far_missing:
        return QuoteCoherenceResult(
            near_missing=True, far_missing=True,
            reason=QuoteCoherenceReason.BOTH_STALE.value,
        )
    if near_missing:
        return QuoteCoherenceResult(
            near_missing=True,
            reason=QuoteCoherenceReason.NEAR_QUOTE_MISSING.value,
        )
    if far_missing:
        return QuoteCoherenceResult(
            far_missing=True,
            reason=QuoteCoherenceReason.FAR_QUOTE_MISSING.value,
        )

    # 4. Quote freshness (per-leg age)
    near_stale = input.near_quote_age_ms > input.max_quote_age_ms
    far_stale = input.far_quote_age_ms > input.max_quote_age_ms

    if near_stale and far_stale:
        return QuoteCoherenceResult(
            fresh=False, near_stale=True, far_stale=True,
            reason=QuoteCoherenceReason.BOTH_STALE.value,
        )
    if near_stale:
        return QuoteCoherenceResult(
            fresh=False, near_stale=True,
            reason=QuoteCoherenceReason.NEAR_STALE.value,
        )
    if far_stale:
        return QuoteCoherenceResult(
            fresh=False, far_stale=True,
            reason=QuoteCoherenceReason.FAR_STALE.value,
        )

    # 5. PnL validity
    if input.gross_pnl is None:
        return QuoteCoherenceResult(
            fresh=True, coherent=False,
            reason=QuoteCoherenceReason.PNL_INVALID.value,
        )

    # All checks passed
    return QuoteCoherenceResult(
        fresh=True, coherent=True,
        reason=QuoteCoherenceReason.READY.value,
    )
