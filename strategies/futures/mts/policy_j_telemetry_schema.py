# 2026-07-26 Gemini CLI: Wave J1.5-A Immutable Policy J Shadow Telemetry Contract & Schema
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class EligibilityReason(str, Enum):
    """Enumeration of Policy J Shadow Mode eligibility reasons."""
    HEDGED_PAIR_SPREAD = "HEDGED_PAIR_SPREAD"
    NOT_SPREAD_PHASE = "NOT_SPREAD_PHASE"
    SINGLE_LEG_ONLY = "SINGLE_LEG_ONLY"
    EXIT_INFLIGHT = "EXIT_INFLIGHT"
    NEAR_QUOTE_STALE = "NEAR_QUOTE_STALE"
    FAR_QUOTE_STALE = "FAR_QUOTE_STALE"
    BOTH_QUOTES_STALE = "BOTH_QUOTES_STALE"
    POLICY_DISABLED = "POLICY_DISABLED"


class PolicyJShadowSignal(str, Enum):
    """Enumeration of Policy J Shadow Signals."""
    NO_SIGNAL = "NO_SIGNAL"
    MONITORING = "MONITORING"
    ARMED = "ARMED"
    WOULD_EXIT_BOTH = "WOULD_EXIT_BOTH"


@dataclass(frozen=True)
class PolicyJShadowSnapshot:
    """
    Immutable telemetry snapshot for Policy J Shadow Mode.
    Pure value object — 100% read-only, fully serializable, zero order execution capability.
    
    Schema Versioning SemVer Rule:
    - 1.x: Backward compatible schema additions.
    - 2.x: Breaking schema changes requiring major Dashboard update.
    """
    schema_version: str = "1.1"
    snapshot_id: str = ""               # Unique snapshot UUID / identifier
    sequence_no: int = 0                # Monotonic sequence counter per trade lifecycle
    trade_id: str | None = None
    event_time: str = ""                # ISO 8601 timestamp string
    processed_at: str = ""              # ISO 8601 timestamp string
    mode: str = "SHADOW_ONLY"           # "SHADOW_ONLY", "EXECUTION_DISABLED"
    eligible: bool = False
    eligibility_reason: str = EligibilityReason.NOT_SPREAD_PHASE.value
    gross_liquidation_pnl_twd: float | None = None
    estimated_friction_twd: float | None = None
    estimated_net_exit_pnl_twd: float | None = None
    peak_net_exit_pnl_twd: float | None = None
    activation_net_pnl_twd: float = 300.0
    giveback_twd: float = 100.0
    would_trigger: bool = False
    execution_blocked: bool = True       # Hardcoded True for safety gate
    near_quote_age_ms: int | None = None
    far_quote_age_ms: int | None = None
    config_hash: str = ""
    shadow_signal: str = PolicyJShadowSignal.NO_SIGNAL.value
    first_trigger_event: bool = False

    def __post_init__(self):
        """Auto-generate snapshot_id and enforce eligibility constructor invariants."""
        if not self.snapshot_id:
            object.__setattr__(self, "snapshot_id", str(uuid.uuid4()))

        # Constructor validation: eligible=True MUST map to HEDGED_PAIR_SPREAD, and vice versa
        if self.eligible and self.eligibility_reason != EligibilityReason.HEDGED_PAIR_SPREAD.value:
            raise ValueError(
                f"Invalid snapshot: eligible=True requires eligibility_reason={EligibilityReason.HEDGED_PAIR_SPREAD.value}, "
                f"got '{self.eligibility_reason}'"
            )
        if not self.eligible and self.eligibility_reason == EligibilityReason.HEDGED_PAIR_SPREAD.value:
            raise ValueError(
                f"Invalid snapshot: eligible=False cannot have eligibility_reason={EligibilityReason.HEDGED_PAIR_SPREAD.value}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Convert snapshot to dictionary for JSON serialization."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize snapshot to JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyJShadowSnapshot":
        """Deserialize dictionary to immutable PolicyJShadowSnapshot."""
        return cls(**data)

    def compute_snapshot_hash(self) -> str:
        """Compute deterministic SHA-256 hash of the snapshot content."""
        content_str = (
            f"{self.schema_version}:{self.snapshot_id}:{self.sequence_no}:"
            f"{self.trade_id}:{self.event_time}:{self.would_trigger}:{self.config_hash}"
        )
        return hashlib.sha256(content_str.encode("utf-8")).hexdigest()[:16]


def compute_policy_j_config_hash(config_dict: dict[str, Any]) -> str:
    """
    Compute canonical SHA-256 hash of Policy J configuration parameters.
    Keys are sorted and float values are normalized to 4 decimal places.
    """
    normalized = {}
    for k, v in sorted(config_dict.items()):
        if isinstance(v, float):
            normalized[k] = round(v, 4)
        else:
            normalized[k] = v
    raw_str = json.dumps(normalized, sort_keys=True)
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:12]
