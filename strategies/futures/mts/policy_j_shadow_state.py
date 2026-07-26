# 2026-07-26 Gemini CLI: Wave J1.5-B Immutable Policy J Shadow State
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PolicyJShadowState:
    """
    Immutable state tracking for Policy J Shadow Mode evaluator.
    Maintains trade lifecycle identity, running peak Net Exit PnL, sequence counter, and trigger emission status.
    """
    trade_id: str | None = None
    peak_net_exit_pnl_twd: float | None = None
    sequence_no: int = 0
    armed: bool = False
    would_trigger_emitted: bool = False
    last_event_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert state to dictionary for JSON serialization/persistence."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyJShadowState":
        """Restore state from dictionary for PM2 restart recovery."""
        return cls(**data)

    def with_trade(self, trade_id: str | None) -> "PolicyJShadowState":
        """Return new state for a new trade lifecycle, resetting peak and sequence to 0."""
        if trade_id == self.trade_id:
            return self
        return PolicyJShadowState(
            trade_id=trade_id,
            peak_net_exit_pnl_twd=None,
            sequence_no=0,
            armed=False,
            would_trigger_emitted=False,
            last_event_key=None,
        )
