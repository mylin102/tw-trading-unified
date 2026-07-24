# 2026-07-17 Gemini CLI: Define custom exceptions for the replay sandbox kernel

from core.trajectory.errors import TrajectoryError

class SandboxError(TrajectoryError):
    """Base class for all sandbox execution errors."""
    pass

class ClockRegressionError(SandboxError):
    """Raised when replay clock is updated with a past timestamp."""
    pass

class InvariantViolationError(SandboxError):
    """Raised when sandbox state invariants are violated (e.g. invalid FSM sequence)."""
    pass

class MissingPortError(SandboxError):
    """Raised when a required Port/Protocol is not provided to the sandbox."""
    pass
