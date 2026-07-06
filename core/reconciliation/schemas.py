from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ReconcileStatus(Enum):
    OK = "OK"
    MISMATCH = "MISMATCH"


class ReconcileSeverity(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class PositionState:
    """Represents a simplified position state for reconciliation."""
    qty: int
    avg_price: float = 0.0
    symbol: str = ""
    source: str = ""


@dataclass
class ReconcileResult:
    status: ReconcileStatus
    severity: Optional[ReconcileSeverity] = None
    reason: str = ""
    details: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == ReconcileStatus.OK

    @property
    def is_critical(self) -> bool:
        return self.severity == ReconcileSeverity.CRITICAL
