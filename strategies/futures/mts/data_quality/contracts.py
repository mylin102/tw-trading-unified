# 2026-07-27 Gemini CLI: DTI-001C Capture Data Quality Contracts & Schemas
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class QualityRating(str, Enum):
    CAPTURE_VALID = "CAPTURE_VALID"
    CAPTURE_DEGRADED = "CAPTURE_DEGRADED"
    CAPTURE_INVALID = "CAPTURE_INVALID"


class CloseStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED_CLEANLY = "CLOSED_CLEANLY"
    INTERRUPTED = "INTERRUPTED"
    CRASH_RECOVERED = "CRASH_RECOVERED"


@dataclass(frozen=True)
class GenerationManifest:
    generation_id: str
    schema_version: str = "1.0.0"
    capture_contract_version: str = "1.0.0"
    host_id: str = ""
    deployment_role: str = "PAPER_TRADER"
    git_sha: str = ""
    git_dirty: bool = False
    process_start_time: Optional[str] = None
    writer_start_time: Optional[str] = None
    writer_stop_time: Optional[str] = None
    shutdown_reason: Optional[str] = None
    pm2_restart_id: Optional[int] = None
    near_contract_code: str = "TMF"
    far_contract_code: str = "TMFI6"
    session_date: str = ""
    output_files: List[str] = field(default_factory=list)
    file_sha256: str = ""
    row_count: int = 0
    first_event_time: Optional[str] = None
    last_event_time: Optional[str] = None
    first_received_at: Optional[str] = None
    last_received_at: Optional[str] = None
    drop_counter_start: int = 0
    drop_counter_end: int = 0
    close_status: str = CloseStatus.CLOSED_CLEANLY.value

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubsystemRatings:
    overall_rating: str
    integrity_rating: str
    ordering_rating: str
    freshness_rating: str
    writer_rating: str
    generation_rating: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QualityAuditSummary:
    generation_id: str
    ratings: SubsystemRatings
    total_rows_inspected: int
    exact_duplicates_count: int
    semantic_duplicates_count: int
    near_tick_monotonic_rate: float
    far_tick_monotonic_rate: float
    global_event_time_regression_rate: float
    micro_reorder_count: int  # <= 100ms
    network_reorder_count: int  # 100ms - 1000ms
    severe_regression_count: int  # > 1000ms
    pair_skew_p50_ms: float
    pair_skew_p90_ms: float
    pair_skew_p99_ms: float
    unexpected_writer_gaps_count: int
    cross_generation_contamination_count: int
    cumulative_drops_count: int

    def to_dict(self) -> Dict[str, Any]:
        res = asdict(self)
        res["ratings"] = self.ratings.to_dict()
        return res
