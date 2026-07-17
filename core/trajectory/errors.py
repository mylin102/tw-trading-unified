# 2026-07-17 Gemini CLI: Define custom exceptions for trajectory contract validation

class TrajectoryError(Exception):
    """Base class for all trajectory replay errors."""
    pass

class TrajectorySchemaError(TrajectoryError):
    """Raised when the event JSON schema does not conform to the contract."""
    pass

class TrajectoryValidationError(TrajectoryError):
    """Raised when event enums, values, or constraints are violated."""
    pass

class DuplicateEventError(TrajectoryError):
    """Raised when duplicate event IDs are detected in the trajectory log."""
    pass

class ManifestVerificationError(TrajectoryError):
    """Raised when dataset manifest verification fails (e.g. content hash mismatch)."""
    pass

class ReferenceIntegrityError(TrajectoryError):
    """Raised when session or trade reference constraints are violated."""
    pass
