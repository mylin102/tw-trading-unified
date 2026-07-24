# 2026-07-17 Gemini CLI: Implement Replay Clock for strict virtual time tracking
from __future__ import annotations

from core.trajectory.sandbox_errors import ClockRegressionError

class ReplayClock:
    """
    Clock that advances only based on event timestamps.
    Prevents time regression and guarantees deterministic event time replay.
    """
    def __init__(self, initial_ns: int = 0):
        self._current_ns = initial_ns

    @property
    def now_ns(self) -> int:
        return self._current_ns

    def advance_to(self, target_ns: int) -> None:
        """
        Advance virtual clock to target timestamp in nanoseconds.
        Raises ClockRegressionError if target timestamp is in the past.
        """
        if target_ns < self._current_ns:
            raise ClockRegressionError(
                f"Clock regression detected: target {target_ns} ns is earlier than current {self._current_ns} ns"
            )
        self._current_ns = target_ns
