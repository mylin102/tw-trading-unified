# 2026-07-17 Gemini CLI: CounterfactualService implementation with side-effect isolation and immutable contracts.

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import os
import subprocess

from core.replay_contracts import build_replay_cases, DecisionReplayCase
from core.replay_release import replay_batch, build_reproduction_report, ReplayResult

@dataclass(frozen=True)
class ReplayConfig:
    """Immutable config override for replay runs."""
    release_stop_threshold: Optional[float] = None
    # Placeholder fields for future parameter extensions (Wave 3)
    confirm_ms: Optional[int] = None
    confirm_ticks: Optional[int] = None

@dataclass(frozen=True)
class MismatchDetail:
    """Detailed info for a mismatched decision case."""
    trade_id: str
    decision_seq: int
    mismatch_category: str
    recorded_action: str
    recorded_leg: Optional[str]
    replayed_action: Optional[str]
    replayed_leg: Optional[str]

@dataclass(frozen=True)
class ReplayMetrics:
    """Aggregated match rates and counts."""
    total_cases: int
    eligible_cases: int
    action_match_rate: float
    leg_match_rate: float
    reason_match_rate: float
    mismatch_count: int
    category_counts: dict[str, int]

@dataclass(frozen=True)
class ProvenanceBundle:
    """Metadata verifying data and system version ancestry."""
    dataset_contract_version: str
    research_methodology_version: str
    dataset_build_id: str
    git_commit: str
    git_repo_state: str  # CLEAN or DIRTY
    generated_time: str

@dataclass(frozen=True)
class PointReplayResult:
    """Returned response for a point replay execution."""
    metrics: ReplayMetrics
    mismatches: list[MismatchDetail]
    provenance: ProvenanceBundle

class CounterfactualService:
    """Service facade providing side-effect-free replay execution."""

    def __init__(self, data_path: Optional[Path] = None):
        self._data_path = data_path

    def run_point_replay(self, config: Optional[ReplayConfig] = None) -> PointReplayResult:
        """Run point replay on compiled trades using the optional config override."""
        # 1. Fetch cases
        cases = build_replay_cases(path=self._data_path)
        
        # 2. Run simulation
        # In Wave 1 we pass cases to replay_batch (we'll update core/replay_release to support config)
        results = replay_batch(cases, config=config)
        report = build_reproduction_report(results)
        
        # 3. Collect mismatches
        mismatch_details = []
        for r in results:
            if not (r.action_match and r.leg_match and r.reason_match):
                mismatch_details.append(
                    MismatchDetail(
                        trade_id=r.trade_id,
                        decision_seq=r.decision_seq,
                        mismatch_category=r.mismatch_category,
                        recorded_action=r.recorded_action,
                        recorded_leg=r.recorded_release_leg,
                        replayed_action=r.replayed_action,
                        replayed_leg=r.replayed_release_leg,
                    )
                )

        # 4. Resolve provenance
        git_commit = "UNKNOWN"
        git_repo_state = "UNKNOWN"
        try:
            git_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            ).decode("utf-8").strip()
            
            status_out = subprocess.check_output(
                ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL
            ).decode("utf-8").strip()
            git_repo_state = "DIRTY" if status_out else "CLEAN"
        except Exception:
            pass

        # Load manifest to get build id
        build_id = "UNKNOWN"
        try:
            # Reconstruct default folder if path is None
            manifest_path = (self._data_path or Path("data/current")) / "trade_dataset_manifest.json"
            if manifest_path.exists():
                import json
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                    build_id = manifest.get("dataset_build_id", "UNKNOWN")
        except Exception:
            pass

        provenance = ProvenanceBundle(
            dataset_contract_version="v1.0.0",
            research_methodology_version="v1.0.0",
            dataset_build_id=build_id,
            git_commit=git_commit,
            git_repo_state=git_repo_state,
            generated_time=datetime.utcnow().isoformat() + "Z",
        )

        metrics = ReplayMetrics(
            total_cases=report.get("total_cases", 0),
            eligible_cases=report.get("total_cases", 0), # in release, all tested cases are eligible
            action_match_rate=report.get("action_match_rate", 0.0) / 100.0, # convert 98.2 to 0.982
            leg_match_rate=report.get("leg_match_rate", 0.0) / 100.0,
            reason_match_rate=report.get("reason_match_rate", 0.0) / 100.0,
            mismatch_count=report.get("mismatch_count", 0),
            category_counts=report.get("category_counts", {}),
        )

        return PointReplayResult(
            metrics=metrics,
            mismatches=mismatch_details,
            provenance=provenance,
        )
