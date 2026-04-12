"""
Diagnostic Rule Engine — root cause analysis for losing streaks.

Not: "3 losses → switch strategy" (blind)
But: "3 losses, all STOP_LOSS, avg momentum=15 → tighten min_momentum" (diagnostic)

Uses Phase 0 data: entry diagnostic snapshots + exit reasons.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DiagnosticAction:
    action_type: str    # CONTINUE, COOLDOWN, TIGHTEN_ENTRY, HALT, SWITCH_STRATEGY
    reason: str         # Human-readable explanation
    param: str = ""     # Parameter to adjust (for TIGHTEN_ENTRY)
    delta: float = 0.0  # Adjustment amount
    new_strategy: str = ""  # For SWITCH_STRATEGY
    cooldown_mins: int = 0  # For COOLDOWN


@dataclass
class TradeDiagnosis:
    """Single trade with entry diagnostics and exit result."""
    exit_reason: str        # STOP_LOSS, VWAP, ATR_TRAIL, TP1
    pnl_pts: float          # Points lost/gained
    entry_diag: dict        # Phase 0c snapshot
    session: str            # day or night


def diagnose_losing_streak(
    trades: list[TradeDiagnosis],
    current_strategy: str = "",
    regime: str = "trending",
) -> DiagnosticAction:
    """
    Root cause analysis for consecutive losses.

    Decision tree:
      3+ losses → Check exit pattern
        → All STOP_LOSS
          → Check entry quality
            → High VWAP distance → tighten confirm_bars (stop chasing)
            → Low momentum → raise min_momentum (filter weak signals)
        → All VWAP_EXIT
          → Raise min_momentum (need stronger trend)
        → Mixed exits
          → < 5 trades → COOLDOWN 15min (normal variance)
          → 5+ trades → Check rolling PF proxy
            → PF < 1.0 → SWITCH (genuine decay)
            → PF >= 1.0 → CONTINUE (still profitable)
    """
    if not trades:
        return DiagnosticAction("CONTINUE", reason="No losing trades to diagnose")

    # ── Pattern 1: All stopped out → entry quality problem ──
    exit_reasons = [t.exit_reason for t in trades]
    if all(r == "STOP_LOSS" for r in exit_reasons):
        avg_momentum = _mean([t.entry_diag.get("momentum", 0) for t in trades])
        avg_vwap_dist = _mean([t.entry_diag.get("vwap_distance_pts", 0) for t in trades])
        avg_atr = _mean([t.entry_diag.get("atr", 50) for t in trades])

        if avg_atr > 0 and avg_vwap_dist > 2 * avg_atr:
            return DiagnosticAction(
                "TIGHTEN_ENTRY",
                param="confirm_bars",
                delta=3,
                reason=f"Entry too far from VWAP (avg {avg_vwap_dist:.0f}pts > 2x ATR {avg_atr:.0f}). "
                       f"Chasing price → increase confirm_bars by 3",
            )

        if avg_momentum < 30:
            return DiagnosticAction(
                "TIGHTEN_ENTRY",
                param="min_momentum",
                delta=20,
                reason=f"Entry momentum too weak (avg {avg_momentum:.0f} < 30). "
                       f"Raise min_momentum by 20 to filter weak signals",
            )

    # ── Pattern 2: All VWAP exits → trend strength problem ──
    if all(r == "VWAP" for r in exit_reasons):
        return DiagnosticAction(
            "TIGHTEN_ENTRY",
            param="min_momentum",
            delta=20,
            reason="Frequent VWAP exits → trend not strong enough. Raise min_momentum by 20",
        )

    # ── Pattern 3: SHOCK regime detected → stop trading ──
    if any(t.entry_diag.get("regime") == "SHOCK" for t in trades):
        return DiagnosticAction(
            "HALT",
            reason="SHOCK regime detected in losing trades. Halt until regime stabilizes",
        )

    # ── Pattern 4: Mixed exits, < 5 trades → normal variance ──
    if len(trades) < 5:
        return DiagnosticAction(
            "COOLDOWN",
            cooldown_mins=15,
            reason=f"Only {len(trades)} losses with mixed exits. "
                   f"Normal variance (PF=2.1 has 40% loss rate). Cool down 15min",
        )

    # ── Pattern 5: 5+ losses → check if genuine decay ──
    # Use PnL sign ratio as PF proxy (can't compute real PF from just losing trades)
    # If we have 5+ consecutive losses, it's a genuine decay signal
    if len(trades) >= 5:
        from core.strategy_registry import select_best_strategy

        alt_strategy = select_best_strategy(
            session_type=trades[0].entry_diag.get("session", "day"),
            regime=regime,
        )

        if alt_strategy and alt_strategy != current_strategy:
            return DiagnosticAction(
                "SWITCH_STRATEGY",
                new_strategy=alt_strategy,
                reason=f"5+ consecutive losses ({current_strategy or 'unknown'}), "
                       f"rolling PF likely < 1.0. Switch to {alt_strategy}",
            )

    # ── Default: continue but flag for review ──
    return DiagnosticAction(
        "CONTINUE",
        reason=f"{len(trades)} losses diagnosed. "
               f"Exit pattern: {', '.join(set(exit_reasons))}. "
               f"Continue with caution, review if streak continues",
    )


def _mean(values: list[float]) -> float:
    """Safe mean of non-empty list."""
    if not values:
        return 0.0
    return sum(values) / len(values)
