"""
Signal dataclass — strategy output contract.
Replaces the ad-hoc dict convention with a typed, validated structure.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Signal:
    """Strategy output signal.

    SDD Rule 2 (Side Effects After Validation):
        Signals must pass ``validate()`` before any state mutation or file write.

    Backward compatibility:
        Call ``.to_dict()`` to get the legacy dict format used by existing monitors.
    """

    action: str            # "BUY" | "SELL" | "EXIT" | "PARTIAL_EXIT"
    reason: str            # e.g. "COUNTER_VWAP", "SPRING", "UPTHRUST"
    stop_loss: float       # Absolute price level (not points)
    target: float = 0.0    # Optional take-profit target price
    confidence: float = 1.0  # 0.0–1.0, for strategy weighting

    _VALID_ACTIONS = frozenset({"BUY", "SELL", "EXIT", "PARTIAL_EXIT"})

    # ── Validation ───────────────────────────────────────────────────────

    def validate(self) -> tuple[bool, str]:
        """Check signal integrity.

        Returns
        -------
        (True, "")   if valid
        (False, msg) if invalid — *msg* explains why
        """
        if self.action not in self._VALID_ACTIONS:
            return False, f"Invalid action: {self.action!r}"
        if not self.reason:
            return False, "Missing reason"
        if self.action in ("BUY", "SELL") and self.stop_loss <= 0:
            return False, f"Invalid stop_loss for {self.action}: {self.stop_loss}"
        if not (0.0 <= self.confidence <= 1.0):
            return False, f"Confidence out of range: {self.confidence}"
        return True, ""

    # ── Backward compatibility ───────────────────────────────────────────

    def to_dict(self) -> dict:
        """Return legacy dict format for existing monitor code."""
        return {
            "action": self.action,
            "reason": self.reason,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "confidence": self.confidence,
        }
