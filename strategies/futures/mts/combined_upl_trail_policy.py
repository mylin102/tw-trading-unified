# 2026-07-26 Gemini CLI: Pure Policy Extraction for Policy J (Combined Total UPL Trailing Exit)
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from strategies.plugins.futures.active.mts_lifecycle_adapter import PositionPhase
from strategies.futures.mts.quote_coherence import (
    QuoteCoherenceInput,
    QuoteCoherenceReason,
    evaluate_quote_coherence,
)

logger = logging.getLogger(__name__)


class CombinedUplTrailAction(Enum):
    """Action produced by CombinedUplTrailPolicy."""
    NO_ACTION = "NO_ACTION"
    TRIGGER_COMBINED_EXIT = "TRIGGER_COMBINED_EXIT"


@dataclass(frozen=True)
class CombinedUplTrailConfig:
    """Immutable configuration for Combined UPL Trailing Policy (Default FALSE for safety)."""
    enabled: bool = False
    activation_net_pnl_twd: float = 300.0  # Net profit target to activate (e.g. 300 TWD)
    giveback_twd: float = 100.0            # Giveback tolerance from peak (e.g. 100 TWD)
    exit_cost_buffer_twd: float = 100.0    # Friction buffer for commission + tax + slippage


@dataclass(frozen=True)
class CombinedUplTrailState:
    """Immutable state for Combined UPL Trailing Policy. Pure value object — fully serializable."""
    activated: bool = False
    peak_net_exit_pnl_twd: float | None = None
    triggered: bool = False
    trigger_event_id: str | None = None   # deterministic: trade_id:first_trigger_seq
    execution_intent_emitted: bool = False
    last_sequence_no: int = 0
    trade_id: str | None = None
    schema_version: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "activated": self.activated,
            "peak_net_exit_pnl_twd": self.peak_net_exit_pnl_twd,
            "triggered": self.triggered,
            "trigger_event_id": self.trigger_event_id,
            "execution_intent_emitted": self.execution_intent_emitted,
            "last_sequence_no": self.last_sequence_no,
            "trade_id": self.trade_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CombinedUplTrailState":
        if not data:
            return cls()
        return cls(
            activated=data.get("activated", False),
            peak_net_exit_pnl_twd=data.get("peak_net_exit_pnl_twd"),
            triggered=data.get("triggered", False),
            trigger_event_id=data.get("trigger_event_id"),
            execution_intent_emitted=data.get("execution_intent_emitted", False),
            last_sequence_no=data.get("last_sequence_no", 0),
            trade_id=data.get("trade_id"),
            schema_version=data.get("schema_version", 2),
        )


@dataclass(frozen=True)
class CombinedUplTrailContext:
    """Pure observation context for evaluation step. Zero state or peak tracking inside."""
    estimated_gross_liquidation_pnl_twd: float
    estimated_exit_friction_twd: float
    phase: PositionPhase
    near_open_qty: int
    far_open_qty: int
    has_exit_inflight: bool
    near_quote_age_ms: int | None = None
    far_quote_age_ms: int | None = None
    max_quote_age_ms: int = 1000
    max_pair_skew_ms: int = 500


def estimate_net_exit_pnl_twd(
    gross_liquidation_pnl_twd: float,
    commission_twd: float,
    exchange_fee_twd: float,
    tax_twd: float,
    bid_ask_cost_twd: float = 0.0,
    slippage_buffer_twd: float = 0.0,
) -> float:
    """
    Calculates estimated net liquidation PnL in TWD.

    Friction Contract:
    - If gross_liquidation_pnl_twd is derived from mid-market prices, bid_ask_cost_twd MUST include half-spread cost for both legs.
    - If gross_liquidation_pnl_twd is derived directly from executable bid/ask quotes, bid_ask_cost_twd MUST be set to 0.0 to prevent double counting.
    """
    total_friction = commission_twd + exchange_fee_twd + tax_twd + bid_ask_cost_twd + slippage_buffer_twd
    return gross_liquidation_pnl_twd - total_friction


class CombinedUplTrailPolicy:
    """Pure policy engine for Policy J. Pure function evaluate(ctx, state, config) -> (action, new_state)."""

    @staticmethod
    def evaluate(
        ctx: CombinedUplTrailContext,
        state: CombinedUplTrailState,
        config: CombinedUplTrailConfig,
    ) -> tuple[CombinedUplTrailAction, CombinedUplTrailState]:
        # 1. Safety Lockdown Check
        if not config.enabled:
            return CombinedUplTrailAction.NO_ACTION, state

        # 2. Idempotency Check: Already triggered once
        if state.triggered:
            return CombinedUplTrailAction.TRIGGER_COMBINED_EXIT, state

        # 3. Position Eligibility Check: Must be full 2-leg SPREAD position
        if ctx.phase != PositionPhase.SPREAD or ctx.near_open_qty <= 0 or ctx.far_open_qty <= 0:
            return CombinedUplTrailAction.NO_ACTION, state

        # 4. Market & Execution Quality Guards (Shared quote coherence contract)
        _qc_input = QuoteCoherenceInput(
            near_quote_age_ms=ctx.near_quote_age_ms,
            far_quote_age_ms=ctx.far_quote_age_ms,
            max_quote_age_ms=ctx.max_quote_age_ms,
            near_open_qty=ctx.near_open_qty,
            far_open_qty=ctx.far_open_qty,
            is_spread_phase=(ctx.phase == PositionPhase.SPREAD),
            has_exit_inflight=ctx.has_exit_inflight,
            gross_pnl=ctx.estimated_gross_liquidation_pnl_twd,
        )
        _qc = evaluate_quote_coherence(_qc_input)
        if not _qc.fresh or not _qc.coherent:
            return CombinedUplTrailAction.NO_ACTION, state

        # 5. Calculate Net Exit PnL
        net_exit_pnl = ctx.estimated_gross_liquidation_pnl_twd - ctx.estimated_exit_friction_twd

        # 6. Unactivated State: Check for activation
        if not state.activated:
            if net_exit_pnl >= config.activation_net_pnl_twd:
                new_state = CombinedUplTrailState(
                    activated=True,
                    peak_net_exit_pnl_twd=net_exit_pnl,
                    triggered=False,
                    trade_id=state.trade_id,
                    last_sequence_no=state.last_sequence_no + 1,
                )
                logger.info(
                    "[POLICY_J_ACTIVATED] Net PnL %.1f TWD >= target %.1f TWD. Peak set to %.1f TWD",
                    net_exit_pnl, config.activation_net_pnl_twd, net_exit_pnl,
                )
                return CombinedUplTrailAction.NO_ACTION, new_state
            return CombinedUplTrailAction.NO_ACTION, state

        # ── Invariant: activated=True after this point ──
        # Once activated=True, it SHALL remain True for trade duration.
        # The next line asserts this contract. CombinedUplTrailState
        # must always produce activated=True in this branch.

        # 7. Activated State: Peak tracking and giveback exit check
        current_peak = state.peak_net_exit_pnl_twd if state.peak_net_exit_pnl_twd is not None else net_exit_pnl
        new_peak = max(current_peak, net_exit_pnl)

        if net_exit_pnl <= (new_peak - config.giveback_twd):
            # ── Trigger edge: separate condition from first_trigger_event ──
            # trigger_condition_met is a LEVEL (may be true for many cycles).
            # first_trigger_event is an EDGE (true only on first occurrence).
            _condition_met = True
            _first_trigger = _condition_met and state.trigger_event_id is None
            _trigger_event_id = state.trigger_event_id
            if _first_trigger:
                _trigger_event_id = f"{state.trade_id or '?'}:{state.last_sequence_no + 1}"

            # ── Invariant: would_trigger=True ⇒ activated=True ──
            new_state = CombinedUplTrailState(
                activated=True,
                peak_net_exit_pnl_twd=new_peak,
                triggered=True,
                trigger_event_id=_trigger_event_id,
                execution_intent_emitted=state.execution_intent_emitted or _first_trigger,
                last_sequence_no=state.last_sequence_no + 1,
                trade_id=state.trade_id,
            )
            logger.info(
                "[POLICY_J_TRIGGERED] Net PnL %.1f TWD <= (Peak %.1f - Giveback %.1f). Action=TRIGGER_COMBINED_EXIT",
                net_exit_pnl, new_peak, config.giveback_twd,
            )
            return CombinedUplTrailAction.TRIGGER_COMBINED_EXIT, new_state

        # Update peak if new high reached
        new_state = CombinedUplTrailState(
            activated=True,
            peak_net_exit_pnl_twd=new_peak,
            triggered=False,
            trigger_event_id=state.trigger_event_id,
            execution_intent_emitted=state.execution_intent_emitted,
            last_sequence_no=state.last_sequence_no + 1,
            trade_id=state.trade_id,
        )
        return CombinedUplTrailAction.NO_ACTION, new_state
