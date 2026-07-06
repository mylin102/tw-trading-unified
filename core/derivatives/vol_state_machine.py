"""
Volatility State Machine — hysteresis-stabilized volatility regime.

PURPOSE
-------
Raw IV surface signals (directional_skew, tension, percentile) are extremely
noisy at tick/bar frequency. Direct consumption causes regime thrashing:
reposition, resize, skip, rebuild strikes every few bars.

This state machine enforces:
  - Entry hysteresis: must sustain conditions for N consecutive samples
  - Exit hysteresis: must sustain exit conditions for M consecutive samples
  - State age tracking: how long we've been in current state
  - Transition counting: how many transitions today
  - Minimum dwell time: prevents rapid oscillation

DESIGN
------
- Stateless inputs: VolatilityContext (from shape_classifier) + percentile data
- Stateful outputs: VolatilityState (smoothed)
- No strategy coupling — pure state inference
- Thread-safe: called from monitor main loop, re-entrant via lock

STATES
------
CALM       — Low IV percentile, low tension, no skew
NORMAL     — Moderate conditions, normal range
EXPANDING  — Tension rising, IV percentile increasing (pre-cursor)
PANIC      — LEFT skew + HIGH tension + high percentile
EUPHORIA   — RIGHT skew + HIGH tension + high percentile
EVENT      — Extreme tension regardless of direction (binary event)
UNKNOWN    — Insufficient data

TRANSITIONS
-----------
Any state → UNKNOWN on insufficient data
UNKNOWN → first stable state after min_samples
All state changes logged explicitly at INFO level.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Optional


# ---------------------------------------------------------------------------
# State Enum
# ---------------------------------------------------------------------------

class VolState(Enum):
    CALM = "CALM"
    NORMAL = "NORMAL"
    EXPANDING = "EXPANDING"
    PANIC = "PANIC"
    EUPHORIA = "EUPHORIA"
    EVENT = "EVENT"
    UNKNOWN = "UNKNOWN"

    def __str__(self):
        return self.value


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

@dataclass
class VolatilityState:
    """Smoothed volatility state — output of VolatilityStateMachine.

    This is what strategies should consume, NOT raw VolatilityContext.

    Fields
    ------
    state: VolState — current stabilized state
    age_sec: int — seconds since entering this state
    entered_at: Optional[datetime] — when this state was entered
    transition_count: int — total transitions since last reset
    persistent: bool — True if state has lasted > age_persistent threshold
    confidence: float — current VolatilityContext confidence (pass-through)
    directional_skew: str — current raw skew direction (pass-through)
    tension: str — current raw tension (pass-through)
    iv_percentile: float — current percentile (pass-through)
    """
    state: VolState = VolState.UNKNOWN
    age_sec: int = 0
    entered_at: Optional[datetime.datetime] = None
    transition_count: int = 0
    persistent: bool = False

    # Pass-through for observability (raw, unsmoothed)
    confidence: float = 0.0
    directional_skew: str = "UNKNOWN"
    tension: str = "UNKNOWN"
    iv_percentile: float = 0.0

    def to_dict(self) -> dict:
        return {
            "state": str(self.state),
            "age_sec": self.age_sec,
            "transition_count": self.transition_count,
            "persistent": self.persistent,
            "confidence": self.confidence,
            "directional_skew": self.directional_skew,
            "tension": self.tension,
            "iv_percentile": self.iv_percentile,
        }


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------

class VolatilityStateMachine:
    """Hysteresis-stabilized volatility state machine.

    Parameters
    ----------
    min_samples_entry: int — samples to sustain before entering a state (default 3)
    min_samples_exit: int — samples to sustain before exiting a state (default 5)
    min_dwell_sec: int — minimum seconds before any transition allowed (default 60)
    age_persistent_sec: int — seconds before state is considered 'persistent' (default 1800)

    panic_pct_threshold: float — IV percentile above this + LEFT/HIGH → PANIC (default 0.85)
    euphoria_pct_threshold: float — IV percentile above this + RIGHT/HIGH → EUPHORIA (default 0.85)
    calm_pct_threshold: float — IV percentile below this + LOW tension → CALM (default 0.30)
    expanding_tension_threshold: str — tension level for EXPANDING (default "MEDIUM")
    event_pct_threshold: float — tension HIGH + percentile very high → EVENT (default 0.95)
    """

    def __init__(
        self,
        min_samples_entry: int = 3,
        min_samples_exit: int = 5,
        min_dwell_sec: int = 60,
        age_persistent_sec: int = 1800,

        panic_pct_threshold: float = 0.85,
        euphoria_pct_threshold: float = 0.85,
        calm_pct_threshold: float = 0.30,
        expanding_tension_threshold: str = "MEDIUM",
        event_pct_threshold: float = 0.95,
    ):
        # Hysteresis parameters
        self.min_samples_entry = min_samples_entry
        self.min_samples_exit = min_samples_exit
        self.min_dwell_sec = min_dwell_sec
        self.age_persistent_sec = age_persistent_sec

        # State threshold parameters
        self.panic_pct_threshold = panic_pct_threshold
        self.euphoria_pct_threshold = euphoria_pct_threshold
        self.calm_pct_threshold = calm_pct_threshold
        self.expanding_tension_threshold = expanding_tension_threshold
        self.event_pct_threshold = event_pct_threshold

        # State
        self._current_state: VolState = VolState.UNKNOWN
        self._entered_at: Optional[datetime.datetime] = None
        self._transition_count: int = 0

        # Hysteresis counters
        self._entry_count: int = 0   # consecutive samples proposing entry
        self._exit_count: int = 0    # consecutive samples proposing exit
        self._last_entry_proposal: str = ""  # which state was being proposed

        # Raw input for reference (pass-through)
        self._last_confidence: float = 0.0
        self._last_directional_skew: str = "UNKNOWN"
        self._last_tension: str = "UNKNOWN"
        self._last_percentile: float = 0.0

        self._lock = Lock()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def update(
        self,
        directional_skew: str,
        tension: str,
        iv_percentile: float,
        confidence: float,
        timestamp: Optional[datetime.datetime] = None,
    ) -> VolatilityState:
        """Update the state machine with a new observation.

        Call this on every bar tick (surface snapshot available).
        Returns the current stabilized VolatilityState.

        Args:
            directional_skew: from VolatilityContext
            tension: from VolatilityContext
            iv_percentile: from IVPercentileEngine (0 if not ready)
            confidence: from VolatilityContext
            timestamp: observation time

        Returns:
            VolatilityState (current, after hysteresis)
        """
        ts = timestamp or datetime.datetime.utcnow()

        with self._lock:
            # Pass through raw values for observability
            self._last_confidence = confidence
            self._last_directional_skew = directional_skew
            self._last_tension = tension
            self._last_percentile = iv_percentile

            # Determine proposed raw state (no hysteresis)
            proposed = self._propose_state(
                directional_skew, tension, iv_percentile, confidence,
            )

            # Apply hysteresis
            new_state = self._apply_hysteresis(proposed, ts)

            # Build output
            age_sec = 0
            if self._entered_at is not None:
                age_sec = int((ts - self._entered_at).total_seconds())

            return VolatilityState(
                state=new_state,
                age_sec=age_sec,
                entered_at=self._entered_at,
                transition_count=self._transition_count,
                persistent=age_sec >= self.age_persistent_sec,
                confidence=confidence,
                directional_skew=directional_skew,
                tension=tension,
                iv_percentile=iv_percentile,
            )

    # ------------------------------------------------------------------
    # Raw state proposal (no hysteresis)
    # ------------------------------------------------------------------

    def _propose_state(
        self,
        directional_skew: str,
        tension: str,
        iv_percentile: float,
        confidence: float,
    ) -> VolState:
        """Determine the raw state from current inputs (no hysteresis).

        Returns the state that would be active if there were no smoothing.
        """
        # UNKNOWN on insufficient data
        if confidence <= 0 or iv_percentile <= 0:
            return VolState.UNKNOWN
        if directional_skew == "UNKNOWN" or tension == "UNKNOWN":
            return VolState.UNKNOWN

        # EVENT: extreme tension, very high percentile, regardless of direction
        if tension == "HIGH" and iv_percentile >= self.event_pct_threshold:
            return VolState.EVENT

        # PANIC: LEFT skew + HIGH tension + high percentile
        if (
            directional_skew == "LEFT"
            and tension == "HIGH"
            and iv_percentile >= self.panic_pct_threshold
        ):
            return VolState.PANIC

        # EUPHORIA: RIGHT skew + HIGH tension + high percentile
        if (
            directional_skew == "RIGHT"
            and tension == "HIGH"
            and iv_percentile >= self.euphoria_pct_threshold
        ):
            return VolState.EUPHORIA

        # EXPANDING: tension rising (medium+) or percentile increasing
        if tension in ("MEDIUM", "HIGH"):
            return VolState.EXPANDING

        # CALM: low percentile, low tension
        if tension == "LOW" and iv_percentile < self.calm_pct_threshold:
            return VolState.CALM

        # Default: NORMAL
        return VolState.NORMAL

    # ------------------------------------------------------------------
    # Hysteresis
    # ------------------------------------------------------------------

    def _apply_hysteresis(self, proposed: VolState, now: datetime.datetime) -> VolState:
        """Apply entry/exit hysteresis to prevent rapid oscillation.

        Entry: must see same proposed state N consecutive times.
        Exit: must see different proposed state M consecutive times.
        Min dwell: no transition allowed within min_dwell_sec of entering.
        """
        current = self._current_state
        proposal_key = str(proposed)

        # Check minimum dwell time
        if self._entered_at is not None:
            dwell_sec = (now - self._entered_at).total_seconds()
            if dwell_sec < self.min_dwell_sec and proposed != current:
                # Not allowed to transition yet
                self._exit_count = 0
                return current

        # --- Entry hysteresis ---
        if proposed != current:
            # Proposing a new state
            if self._last_entry_proposal == proposal_key:
                self._entry_count += 1
            else:
                self._entry_count = 1
                self._last_entry_proposal = proposal_key

            if self._entry_count >= self.min_samples_entry:
                # SUSTAINED: perform transition
                new_state = proposed
                self._transition(new_state, now)
                return new_state
            else:
                # Not yet sustained — stay in current
                self._exit_count = 0
                return current
        else:
            # Same as current — check for exit
            if self._entry_count > 0:
                self._entry_count = 0  # reset entry counter when state stabilizes

            if proposed != current:
                self._exit_count += 1
                if self._exit_count >= self.min_samples_exit:
                    new_state = proposed
                    self._transition(new_state, now)
                    return new_state
            else:
                self._exit_count = 0

            return current

    def _transition(self, new_state: VolState, now: datetime.datetime) -> None:
        """Perform a state transition with logging."""
        old = self._current_state
        self._current_state = new_state
        self._entered_at = now
        self._transition_count += 1
        self._entry_count = 0
        self._exit_count = 0

        import logging
        logger = logging.getLogger(__name__)
        logger.info(
            "[VolStateMachine] TRANSITION: %s → %s (count=%d, "
            "skew=%s tension=%s pct=%.2f)",
            str(old), str(new_state), self._transition_count,
            self._last_directional_skew, self._last_tension,
            self._last_percentile,
        )
        # Also print to console for immediate visibility
        print(
            "[VolStateMachine] TRANSITION: %s → %s (count=%d)" % (
                str(old), str(new_state), self._transition_count,
            ),
            flush=True,
        )

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset state machine to UNKNOWN. Called on session restart."""
        with self._lock:
            self._current_state = VolState.UNKNOWN
            self._entered_at = None
            self._transition_count = 0
            self._entry_count = 0
            self._exit_count = 0
            self._last_entry_proposal = ""
