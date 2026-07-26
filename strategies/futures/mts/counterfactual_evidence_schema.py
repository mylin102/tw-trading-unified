# 2026-07-26 Gemini CLI: Wave J2-A Counterfactual Evidence Schema & Contract
import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional


class ExclusionReason(str, Enum):
    """Exclusion Taxonomy for Counterfactual Evidence Dataset."""
    NONE = "NONE"
    SINGLE_LEG_ONLY = "SINGLE_LEG_ONLY"
    QUOTE_STALE = "QUOTE_STALE"
    RESTART_INCOMPLETE = "RESTART_INCOMPLETE"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    TELEMETRY_GAP = "TELEMETRY_GAP"
    PNL_RECON_MISMATCH = "PNL_RECON_MISMATCH"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


class FillModel(str, Enum):
    """Counterfactual Fill Pricing Model Taxonomy."""
    EXECUTABLE = "EXECUTABLE"      # Bid/Ask executable quote + 1 tick slippage buffer + fees/tax
    CONSERVATIVE = "CONSERVATIVE"  # Bid/Ask executable quote + 2 ticks slippage buffer + fees/tax
    IDEAL = "IDEAL"                # Mid-quote point-in-time valuation (Reference only)


@dataclass(frozen=True)
class CounterfactualTradeFact:
    """
    Trade-Level Counterfactual Evidence Record (Dataset B).
    Evaluates Hypothetical Policy J Exit vs Actual Final Outcome per trade lifecycle.
    """
    trade_id: str
    session_date: str
    session: str                         # "DAY" / "NIGHT"
    direction: str                       # "BUY_NEAR_SELL_FAR" / "SELL_NEAR_BUY_FAR"
    entry_time: str                      # ISO timestamp
    first_trigger_time: Optional[str]    # ISO timestamp or None if never triggered
    activation_twd: float = 300.0
    giveback_twd: float = 100.0
    hypothetical_exit_price_near: Optional[float] = None
    hypothetical_exit_price_far: Optional[float] = None
    hypothetical_net_exit_pnl_twd: Optional[float] = None
    actual_final_net_pnl_twd: float = 0.0
    delta_net_pnl_twd: Optional[float] = None       # Hypothetical Net PnL - Actual Final Net PnL
    actual_mfe_net_pnl_twd: Optional[float] = None
    ped_actual_twd: Optional[float] = None          # Actual MFE Net PnL - Actual Final Net PnL
    ped_policy_j_twd: Optional[float] = None        # Actual MFE Net PnL - Hypothetical Net PnL
    ped_improvement_twd: Optional[float] = None     # PED_actual - PED_policy_j
    trigger_latency_ms: Optional[int] = None
    fill_model: str = FillModel.EXECUTABLE.value
    eligible_for_analysis: bool = True
    exclusion_reason: str = ExclusionReason.NONE.value
    config_hash: str = ""

    def __post_init__(self):
        """Validate constructor invariants between eligible_for_analysis and exclusion_reason."""
        if self.eligible_for_analysis and self.exclusion_reason != ExclusionReason.NONE.value:
            raise ValueError(
                f"Invalid record: eligible_for_analysis=True requires exclusion_reason={ExclusionReason.NONE.value}, "
                f"got '{self.exclusion_reason}'"
            )
        if not self.eligible_for_analysis and self.exclusion_reason == ExclusionReason.NONE.value:
            raise ValueError(
                f"Invalid record: eligible_for_analysis=False cannot have exclusion_reason={ExclusionReason.NONE.value}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Convert record to dictionary for JSON/CSV serialization."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize record to JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CounterfactualTradeFact":
        """Deserialize dictionary to immutable CounterfactualTradeFact."""
        return cls(**data)


def calculate_counterfactual_metrics(
    hypothetical_net_pnl: Optional[float],
    actual_final_pnl: float,
    actual_mfe_pnl: Optional[float],
) -> dict[str, Optional[float]]:
    """
    Calculate primary estimands (ΔNetPnL, PED_actual, PED_policy_j, PED_improvement).
    """
    if hypothetical_net_pnl is None:
        return {
            "delta_net_pnl_twd": None,
            "ped_actual_twd": None,
            "ped_policy_j_twd": None,
            "ped_improvement_twd": None,
        }

    delta_pnl = hypothetical_net_pnl - actual_final_pnl

    ped_actual = (actual_mfe_pnl - actual_final_pnl) if actual_mfe_pnl is not None else None
    ped_policy_j = (actual_mfe_pnl - hypothetical_net_pnl) if actual_mfe_pnl is not None else None
    ped_improvement = (ped_actual - ped_policy_j) if (ped_actual is not None and ped_policy_j is not None) else None

    return {
        "delta_net_pnl_twd": round(delta_pnl, 2),
        "ped_actual_twd": round(ped_actual, 2) if ped_actual is not None else None,
        "ped_policy_j_twd": round(ped_policy_j, 2) if ped_policy_j is not None else None,
        "ped_improvement_twd": round(ped_improvement, 2) if ped_improvement is not None else None,
    }
