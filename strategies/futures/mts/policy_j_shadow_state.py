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

    @classmethod
    def restore_from_jsonl(cls, jsonl_file_path: Any, active_trade_id: str | None) -> "PolicyJShadowState":
        """
        Restore state from JSONL file for PM2 restart recovery.
        Tolerates partial/corrupted final line. Returns default state if file does not exist.
        """
        from pathlib import Path
        import json

        file_path = Path(jsonl_file_path)
        if not file_path.exists() or not active_trade_id:
            return cls(trade_id=active_trade_id)

        last_valid_snapshot = None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                        if data.get("trade_id") == active_trade_id:
                            last_valid_snapshot = data
                    except Exception:
                        # Tolerates partial or corrupted line gracefully
                        continue
        except Exception:
            return cls(trade_id=active_trade_id)

        if not last_valid_snapshot:
            return cls(trade_id=active_trade_id)

        return cls(
            trade_id=active_trade_id,
            peak_net_exit_pnl_twd=last_valid_snapshot.get("peak_net_exit_pnl_twd"),
            sequence_no=int(last_valid_snapshot.get("sequence_no", 0)),
            armed=bool(last_valid_snapshot.get("shadow_signal") in ("ARMED", "WOULD_EXIT_BOTH")),
            would_trigger_emitted=bool(last_valid_snapshot.get("would_trigger", False)),
            last_event_key=f"{active_trade_id}_{last_valid_snapshot.get('sequence_no', 0)}",
        )

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
