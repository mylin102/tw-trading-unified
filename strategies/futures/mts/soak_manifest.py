# 2026-07-24 Gemini CLI: Wave 1D Shadow Soak Immutable Manifest Generator
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .telemetry import EvaluationAccountingSummary, TelemetryDeliveryAccountingSummary


@dataclass(frozen=True)
class CoverageMetrics:
    """Decision cycle coverage breakdown across sessions and scenarios."""
    total_market_callbacks: int = 0
    total_decision_cycles: int = 0
    eligible_decision_cycles: int = 0
    no_op_cycles: int = 0
    near_release_triggers: int = 0
    far_release_triggers: int = 0
    single_leg_cycles: int = 0
    force_exit_triggers: int = 0
    day_session_cycles: int = 0
    night_session_cycles: int = 0
    restart_reconciliation_cases: int = 0


@dataclass(frozen=True)
class PerformanceMetrics:
    """Latency and resource utilization metrics."""
    shadow_eval_p50_us: float = 0.0
    shadow_eval_p95_us: float = 0.0
    shadow_eval_p99_us: float = 0.0
    total_decision_loop_delta_p99_pct: float = 0.0


@dataclass(frozen=True)
class ShadowSoakManifest:
    """Immutable Shadow Soak Manifest artifact for Wave 1E Promotion Gate."""
    wave: str = "1D"
    authority: str = "legacy"
    deployment_id: str = ""
    git_commit: str = ""
    host: str = ""
    started_at_iso: str = ""
    ended_at_iso: str = ""
    evaluation_accounting: EvaluationAccountingSummary = field(default_factory=EvaluationAccountingSummary)
    delivery_accounting: TelemetryDeliveryAccountingSummary = field(default_factory=TelemetryDeliveryAccountingSummary)
    coverage: CoverageMetrics = field(default_factory=CoverageMetrics)
    performance: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    unexplained_mismatches: int = 0
    shadow_caused_orders: int = 0
    shadow_caused_state_commits: int = 0
    shadow_caused_lifecycle_appends: int = 0
    duplicate_legacy_invocations: int = 0
    duplicate_shadow_invocations: int = 0
    unclassified_cycles: int = 0
    not_observed: list[str] = field(default_factory=list)
    manifest_hash: str = ""

    def evaluate_soak_status(self) -> str:
        """Evaluate status: PASS, FAIL, or INCOMPLETE based on fail-closed rules."""
        if (
            self.unexplained_mismatches > 0
            or self.shadow_caused_orders > 0
            or self.shadow_caused_state_commits > 0
            or self.shadow_caused_lifecycle_appends > 0
            or self.duplicate_legacy_invocations > 0
            or self.duplicate_shadow_invocations > 0
            or self.unclassified_cycles > 0
            or not self.evaluation_accounting.is_accounted
            or not self.delivery_accounting.is_accounted
        ):
            return "FAIL"

        if (
            self.coverage.eligible_decision_cycles == 0
            or self.coverage.day_session_cycles == 0
            or self.coverage.night_session_cycles == 0
        ):
            return "INCOMPLETE"

        return "PASS"

    def export_json(self, target_path: Path | str) -> str:
        """Export manifest to formatted JSON file and return JSON string."""
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        data = asdict(self)
        # Attach evaluated soak status
        data["soak_status"] = self.evaluate_soak_status()

        # Compute sha256 hash of contents (excluding manifest_hash itself)
        data_without_hash = dict(data)
        data_without_hash["manifest_hash"] = ""
        manifest_hash = hashlib.sha256(json.dumps(data_without_hash, sort_keys=True).encode("utf-8")).hexdigest()
        
        data["manifest_hash"] = manifest_hash
        json_str = json.dumps(data, indent=2, ensure_ascii=False)
        path.write_text(json_str, encoding="utf-8")
        return json_str
