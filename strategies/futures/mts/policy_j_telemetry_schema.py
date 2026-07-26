# 2026-07-26 Gemini CLI: Wave J1.5-A Immutable Policy J Shadow Telemetry Contract
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PolicyJShadowSnapshot:
    """
    Immutable telemetry snapshot for Policy J Shadow Mode.
    Pure value object — 100% read-only, fully serializable, zero order execution capability.
    """
    schema_version: str = "1.0"
    trade_id: str | None = None
    event_time: str = ""                # ISO format string
    processed_at: str = ""              # ISO format string
    mode: str = "SHADOW_ONLY"           # "SHADOW_ONLY", "EXECUTION_DISABLED"
    eligible: bool = False
    eligibility_reason: str = "NOT_SPREAD_PHASE"
    gross_liquidation_pnl_twd: float | None = None
    estimated_friction_twd: float | None = None
    estimated_net_exit_pnl_twd: float | None = None
    peak_net_exit_pnl_twd: float | None = None
    activation_net_pnl_twd: float = 300.0
    giveback_twd: float = 100.0
    would_trigger: bool = False
    execution_blocked: bool = True       # Hardcoded True for safety gate
    quote_age_ms: int | None = None
    config_hash: str = ""

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
        content_str = f"{self.schema_version}:{self.trade_id}:{self.event_time}:{self.would_trigger}:{self.config_hash}"
        return hashlib.sha256(content_str.encode("utf-8")).hexdigest()[:16]


def compute_policy_j_config_hash(config_dict: dict[str, Any]) -> str:
    """Compute SHA-256 hash of Policy J configuration parameters."""
    raw_str = json.dumps(config_dict, sort_keys=True)
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:12]
