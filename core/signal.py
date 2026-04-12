"""
Unified Signal Definition — The communication bridge between Strategy and Engine.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict

_VALID_ACTIONS = {"BUY", "SELL", "EXIT", "PARTIAL_EXIT"}


@dataclass
class Signal:
    action: str  # BUY, SELL, EXIT, PARTIAL_EXIT
    reason: str
    stop_loss: float = 0.0
    target: float = 0.0
    confidence: float = 0.0
    quantity: int = 1

    def validate(self) -> tuple[bool, str]:
        """Return (is_valid, error_message)."""
        if self.action not in _VALID_ACTIONS:
            return False, f"Invalid action: {self.action}"
        if not self.reason:
            return False, "Missing reason"
        if self.action in ("BUY", "SELL"):
            if self.stop_loss <= 0:
                return False, f"Invalid stop_loss: {self.stop_loss} (must be > 0)"
        if not (0.0 <= self.confidence <= 1.0):
            return False, f"Confidence {self.confidence} out of range [0, 1]"
        return True, ""

    def to_dict(self) -> dict:
        """Dict for JSON/CSV serialization (backward compatible)."""
        return {
            "action": self.action,
            "reason": self.reason,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "confidence": self.confidence,
            "quantity": self.quantity,
        }
