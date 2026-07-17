# 2026-07-17 Gemini CLI: CounterfactualService implementation with fail-fast schema/hash contract verification.

from dataclasses import dataclass, field
import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from enum import Enum
import os
import subprocess
import pandas as pd
import json

from core.replay_contracts import build_replay_cases, DecisionReplayCase
from core.replay_release import replay_batch, build_reproduction_report, ReplayResult

class DatasetContractError(Exception):
    """Raised when the loaded dataset does not match the required schema or content hash."""
    def __init__(self, code: str, expected_contract: str, missing_columns: list[str], dataset_build_id: str, message: str):
        super().__init__(message)
        self.code = code
        self.expected_contract = expected_contract
        self.missing_columns = missing_columns
        self.dataset_build_id = dataset_build_id

class DecisionDriftCategory(str, Enum):
    NONE = "NONE"
    ACTION_DRIFT = "ACTION_DRIFT"
    LEG_DRIFT = "LEG_DRIFT"
    REASON_DRIFT = "REASON_DRIFT"
    TRIGGER_TO_NO_ACTION = "TRIGGER_TO_NO_ACTION"
    NO_ACTION_TO_TRIGGER = "NO_ACTION_TO_TRIGGER"

@dataclass(frozen=True)
class ParameterSpec:
    name: str
    type: type
    min_val: Any
    max_val: Any
    unit: str
    description: str
    default_val: Any

SWEEPABLE_PARAMETERS = {
    "release_stop_threshold": ParameterSpec(
        name="release_stop_threshold",
        type=float,
        min_val=5.0,
        max_val=1000.0,
        unit="pts",
        description="Threshold offset in points to trigger a leg release",
        default_val=130.0,
    ),
    "confirm_ms": ParameterSpec(
        name="confirm_ms",
        type=int,
        min_val=100,
        max_val=5000,
        unit="ms",
        description="Minimum duration in milliseconds to confirm release trigger",
        default_val=800,
    ),
    "confirm_ticks": ParameterSpec(
        name="confirm_ticks",
        type=int,
        min_val=1,
        max_val=50,
        unit="ticks",
        description="Minimum number of ticks to confirm release trigger",
        default_val=5,
    ),
}

@dataclass(frozen=True)
class SweepParameter:
    name: str
    values: tuple[int | float | bool, ...]

@dataclass(frozen=True)
class ReplayConfig:
    """Immutable config override for replay runs."""
    release_stop_threshold: Optional[float] = None
    confirm_ms: Optional[int] = None
    confirm_ticks: Optional[int] = None

@dataclass(frozen=True)
class SweepRequest:
    parameters: tuple[SweepParameter, ...]
    baseline_config: ReplayConfig
    dataset_generation_id: str
    eligibility_policy_version: str

@dataclass(frozen=True)
class CaseSweepRow:
    case_id: str
    parameter_name: str
    parameter_value: int | float | bool
    historical_action: str
    historical_leg: Optional[str]
    historical_reason: Optional[str]
    baseline_action: str
    baseline_leg: Optional[str]
    baseline_reason: Optional[str]
    counterfactual_action: str
    counterfactual_leg: Optional[str]
    counterfactual_reason: Optional[str]
    drift_category: DecisionDriftCategory

@dataclass(frozen=True)
class SweepMetrics:
    parameter_value: int | float | bool
    eligible_cases: int
    historical_action_match_rate: float
    historical_leg_match_rate: float
    historical_reason_match_rate: float
    baseline_action_drift_rate: float
    baseline_leg_drift_rate: float
    baseline_reason_drift_rate: float
    decision_drift_count: int
    unchanged_count: int

@dataclass(frozen=True)
class ParameterSweepResult:
    parameter_name: str
    metrics_rows: list[SweepMetrics]
    case_matrix: list[CaseSweepRow]
    provenance: "ProvenanceBundle"

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
    excluded_cases: int
    eligibility_policy_version: str
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
        """Run point replay on compiled trades using the optional config override.
        Raises DatasetContractError if schema or hash validation fails.
        """
        # 0. Fail-fast Contract Validation
        from core.trade_dataset import load_dataset, load_manifest, _canonical_content_hash
        
        ds = load_dataset(self._data_path)
        snapshots = ds.get("trade_snapshots")
        manifest = load_manifest(self._data_path)
        build_id = manifest.get("dataset_build_id", "UNKNOWN")
        
        if snapshots is None or snapshots.empty:
            raise DatasetContractError(
                code="REPLAY_DATASET_EMPTY",
                expected_contract="v1.x",
                missing_columns=["trade_snapshots"],
                dataset_build_id=build_id,
                message="Dataset is empty or missing snapshots table.",
            )
            
        missing_cols = []
        if "is_decision_point" not in snapshots.columns:
            missing_cols.append("is_decision_point")
            
        if missing_cols:
            raise DatasetContractError(
                code="REPLAY_DATASET_SCHEMA_MISMATCH",
                expected_contract="v1.x",
                missing_columns=missing_cols,
                dataset_build_id=build_id,
                message=f"Dataset schema mismatch: missing required columns: {missing_cols}",
            )
            
        declared_hash = manifest.get("dataset_content_hash")
        if declared_hash:
            actual_hash = _canonical_content_hash(
                ds.get("trade_facts", pd.DataFrame()),
                ds.get("trade_snapshots", pd.DataFrame()),
                ds.get("trade_decisions", pd.DataFrame()),
                ds.get("trade_outcomes", pd.DataFrame()),
            )
            if actual_hash != declared_hash:
                raise DatasetContractError(
                    code="REPLAY_DATASET_HASH_MISMATCH",
                    expected_contract="v1.x",
                    missing_columns=[],
                    dataset_build_id=build_id,
                    message=f"Dataset content hash mismatch! Expected {declared_hash}, got {actual_hash}",
                )

        # 1. Fetch cases
        cases = build_replay_cases(path=self._data_path)
        
        # 2. Run simulation
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

        provenance = ProvenanceBundle(
            dataset_contract_version="v1.0.0",
            research_methodology_version="v1.0.0",
            dataset_build_id=build_id,
            git_commit=git_commit,
            git_repo_state=git_repo_state,
            generated_time=datetime.utcnow().isoformat() + "Z",
        )

        total_cases = len(cases)
        eligible_cases = len(results)
        excluded_cases = total_cases - eligible_cases

        metrics = ReplayMetrics(
            total_cases=total_cases,
            eligible_cases=eligible_cases,
            excluded_cases=excluded_cases,
            eligibility_policy_version="v1.0 (RELEASE ONLY)",
            action_match_rate=report.get("action_match_rate", 0.0) / 100.0,
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

    def run_parameter_sweep(self, request: SweepRequest) -> ParameterSweepResult:
        """Run point replay over a range of parameter values to measure decision drift.
        Enforces whitelisted parameter constraints and semantic baseline validation.
        """
        # 1. Parameter Whitelist Validation
        if not request.parameters:
            raise ValueError("SweepRequest must contain at least one parameter.")
        if len(request.parameters) > 1:
            raise ValueError("Multi-dimensional sweeps are currently not supported (max_dimensions = 1).")
        
        sweep_param = request.parameters[0]
        if sweep_param.name not in SWEEPABLE_PARAMETERS:
            raise ValueError(f"Parameter '{sweep_param.name}' is not in the sweepable whitelist.")
        
        spec = SWEEPABLE_PARAMETERS[sweep_param.name]
        for val in sweep_param.values:
            if not isinstance(val, (int, float, bool)) or isinstance(val, bool) != (spec.type == bool):
                raise TypeError(f"Value '{val}' does not match expected type {spec.type}")
            if not isinstance(val, bool):
                if val < spec.min_val or val > spec.max_val:
                    raise ValueError(f"Value '{val}' is out of bounds [{spec.min_val}, {spec.max_val}] for {sweep_param.name}")

        # 2. Setup baseline cases and baseline reference run
        cases = build_replay_cases(path=self._data_path)
        baseline_results = replay_batch(cases, config=request.baseline_config)
        baseline_by_case = {r.replay_case_id: r for r in baseline_results}

        # 3. Determine baseline value for semantic verification
        baseline_val = getattr(request.baseline_config, sweep_param.name, None)
        if baseline_val is None:
            baseline_val = spec.default_val

        metrics_rows = []
        case_matrix = []

        # 4. Run parameter sweep
        for val in sweep_param.values:
            # Safely create override config
            overrides = {sweep_param.name: val}
            effective_config = dataclasses.replace(request.baseline_config, **overrides)
            
            # Execute counterfactual run
            cf_results = replay_batch(cases, config=effective_config)
            
            drift_count = 0
            unchanged_count = 0
            hist_action_matches = 0
            hist_leg_matches = 0
            hist_reason_matches = 0
            total_eligible = len(cf_results)
            
            iteration_rows = []
            
            for cf in cf_results:
                base = baseline_by_case.get(cf.replay_case_id)
                base_action = base.replayed_action if base else "NONE"
                base_leg = base.replayed_release_leg if base else None
                base_reason = base.replayed_reason if base else None
                
                cf_action = cf.replayed_action or "NONE"
                cf_leg = cf.replayed_release_leg
                cf_reason = cf.replayed_reason
                
                # Classification of Decision Drift Category
                if cf_action == base_action and cf_leg == base_leg and cf_reason == base_reason:
                    drift_cat = DecisionDriftCategory.NONE
                    unchanged_count += 1
                elif base_action == "NONE" and cf_action != "NONE":
                    drift_cat = DecisionDriftCategory.NO_ACTION_TO_TRIGGER
                    drift_count += 1
                elif base_action != "NONE" and cf_action == "NONE":
                    drift_cat = DecisionDriftCategory.TRIGGER_TO_NO_ACTION
                    drift_count += 1
                elif cf_action != base_action:
                    drift_cat = DecisionDriftCategory.ACTION_DRIFT
                    drift_count += 1
                elif cf_leg != base_leg:
                    drift_cat = DecisionDriftCategory.LEG_DRIFT
                    drift_count += 1
                else:
                    drift_cat = DecisionDriftCategory.REASON_DRIFT
                    drift_count += 1
                
                if cf.action_match:
                    hist_action_matches += 1
                if cf.leg_match:
                    hist_leg_matches += 1
                if cf.reason_match:
                    hist_reason_matches += 1
                    
                row = CaseSweepRow(
                    case_id=cf.replay_case_id,
                    parameter_name=sweep_param.name,
                    parameter_value=val,
                    historical_action=cf.recorded_action,
                    historical_leg=cf.recorded_release_leg,
                    historical_reason=cf.recorded_reason,
                    baseline_action=base_action,
                    baseline_leg=base_leg,
                    baseline_reason=base_reason,
                    counterfactual_action=cf_action,
                    counterfactual_leg=cf_leg,
                    counterfactual_reason=cf_reason,
                    drift_category=drift_cat,
                )
                iteration_rows.append(row)
                case_matrix.append(row)

            # Compute rates
            hist_action_match_rate = hist_action_matches / total_eligible if total_eligible > 0 else 0.0
            hist_leg_match_rate = hist_leg_matches / total_eligible if total_eligible > 0 else 0.0
            hist_reason_match_rate = hist_reason_matches / total_eligible if total_eligible > 0 else 0.0
            
            baseline_action_drift_rate = sum(1 for r in iteration_rows if r.drift_category in (DecisionDriftCategory.ACTION_DRIFT, DecisionDriftCategory.TRIGGER_TO_NO_ACTION, DecisionDriftCategory.NO_ACTION_TO_TRIGGER)) / total_eligible if total_eligible > 0 else 0.0
            baseline_leg_drift_rate = sum(1 for r in iteration_rows if r.drift_category == DecisionDriftCategory.LEG_DRIFT) / total_eligible if total_eligible > 0 else 0.0
            baseline_reason_drift_rate = sum(1 for r in iteration_rows if r.drift_category == DecisionDriftCategory.REASON_DRIFT) / total_eligible if total_eligible > 0 else 0.0
            
            metrics_rows.append(
                SweepMetrics(
                    parameter_value=val,
                    eligible_cases=total_eligible,
                    historical_action_match_rate=hist_action_match_rate,
                    historical_leg_match_rate=hist_leg_match_rate,
                    historical_reason_match_rate=hist_reason_match_rate,
                    baseline_action_drift_rate=baseline_action_drift_rate,
                    baseline_leg_drift_rate=baseline_leg_drift_rate,
                    baseline_reason_drift_rate=baseline_reason_drift_rate,
                    decision_drift_count=drift_count,
                    unchanged_count=unchanged_count,
                )
            )

            # Semantic validation: Sweep at baseline parameter value must perfectly replicate baseline (0 drift)
            if abs(val - baseline_val) < 1e-9:
                if drift_count > 0:
                    raise AssertionError(
                        f"Semantic validation failed: Sweep value '{val}' matches baseline parameter value '{baseline_val}', "
                        f"but generated {drift_count} decision drifts! Ensure consistent simulation pathways."
                    )

        # 5. Build Provenance Bundle
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

        # Use same manifest build id
        from core.trade_dataset import load_manifest
        manifest = load_manifest(self._data_path)
        build_id = manifest.get("dataset_build_id", "UNKNOWN")

        provenance = ProvenanceBundle(
            dataset_contract_version="v1.0.0",
            research_methodology_version="v1.0.0",
            dataset_build_id=build_id,
            git_commit=git_commit,
            git_repo_state=git_repo_state,
            generated_time=datetime.utcnow().isoformat() + "Z",
        )

        return ParameterSweepResult(
            parameter_name=sweep_param.name,
            metrics_rows=metrics_rows,
            case_matrix=case_matrix,
            provenance=provenance,
        )

    def export_experiment(self, result: ParameterSweepResult) -> Path:
        """Export experiment artifacts to an immutable subdirectory under reports/research/counterfactual/."""
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        exp_dir = Path("reports/research/counterfactual") / f"exp-{timestamp}"
        exp_dir.mkdir(parents=True, exist_ok=True)

        # 1. manifest.json
        manifest_data = {
            "experiment_id": f"exp-{timestamp}",
            "experiment_type": "DECISION_POINT_PARAMETER_SWEEP",
            "baseline_id": "research-baseline-v1.0",
            "dataset_generation_id": result.provenance.dataset_build_id,
            "git_commit": result.provenance.git_commit,
            "repo_state": result.provenance.git_repo_state,
            "methodology_version": result.provenance.research_methodology_version,
            "parameter_name": result.parameter_name,
            "parameter_values": [row.parameter_value for row in result.metrics_rows],
            "generated_at": result.provenance.generated_time,
        }
        with open(exp_dir / "manifest.json", "w") as f:
            json.dump(manifest_data, f, indent=2)

        # 2. sweep_metrics.csv
        metrics_df = pd.DataFrame([dataclasses.asdict(row) for row in result.metrics_rows])
        metrics_df.to_csv(exp_dir / "sweep_metrics.csv", index=False)

        # 3. case_decision_matrix.parquet
        matrix_df = pd.DataFrame([dataclasses.asdict(row) for row in result.case_matrix])
        matrix_df["drift_category"] = matrix_df["drift_category"].astype(str)
        matrix_df.to_parquet(exp_dir / "case_decision_matrix.parquet", index=False)

        # 4. summary.md
        summary_content = f"""# Counterfactual Sensitivity Analysis Report
## Experiment: {exp_dir.name}
* **Parameter Swept**: `{result.parameter_name}`
* **Dataset Generation ID**: `{result.provenance.dataset_build_id}`
* **Git Commit**: `{result.provenance.git_commit} ({result.provenance.git_repo_state})`
* **Methodology Version**: `{result.provenance.research_methodology_version}`
* **Generated At**: `{result.provenance.generated_time}`

### Summary Metrics Table
{metrics_df.to_markdown(index=False)}

### Conclusion
This decision-point sensitivity analysis identifies the stability threshold boundaries of the trading decision engine under varying parameters.
"""
        with open(exp_dir / "summary.md", "w") as f:
            f.write(summary_content)

        # 5. result.json (full dump)
        class EnhancedJSONEncoder(json.JSONEncoder):
            def default(self, o):
                if dataclasses.is_dataclass(o):
                    return dataclasses.asdict(o)
                if isinstance(o, Enum):
                    return o.value
                return super().default(o)

        with open(exp_dir / "result.json", "w") as f:
            json.dump(dataclasses.asdict(result), f, cls=EnhancedJSONEncoder, indent=2)

        return exp_dir
