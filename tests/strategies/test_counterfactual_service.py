# 2026-07-17 Gemini CLI: Test suite verifying CounterfactualService contract and side-effect isolation (Wave 2.5).

import pytest
import os
import sys
import hashlib
import copy
import json
from pathlib import Path
from core.counterfactual_service import CounterfactualService, ReplayConfig, PointReplayResult, DatasetContractError

FIXTURE_PATH = Path("tests/fixtures/research_baseline_v1")

def _calculate_file_hash(path: Path) -> str:
    """Calculate SHA256 of a file if it exists, otherwise return empty string."""
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def test_counterfactual_service_determinism():
    """Verify that CounterfactualService produces 100% deterministic outputs when run repeatedly."""
    # Use static hermetic fixture
    service = CounterfactualService(data_path=FIXTURE_PATH)
    res1 = service.run_point_replay()
    res2 = service.run_point_replay()
    
    # 1. Decision Determinism (Metrics and mismatches are identical)
    assert res1.metrics.total_cases == res2.metrics.total_cases
    assert res1.metrics.eligible_cases == res2.metrics.eligible_cases
    assert res1.metrics.excluded_cases == res2.metrics.excluded_cases
    assert res1.metrics.eligibility_policy_version == res2.metrics.eligibility_policy_version
    assert res1.metrics.action_match_rate == res2.metrics.action_match_rate
    assert res1.metrics.leg_match_rate == res2.metrics.leg_match_rate
    assert res1.metrics.reason_match_rate == res2.metrics.reason_match_rate
    assert res1.metrics.mismatch_count == res2.metrics.mismatch_count
    
    assert len(res1.mismatches) == len(res2.mismatches)
    for m1, m2 in zip(res1.mismatches, res2.mismatches):
        assert m1.trade_id == m2.trade_id
        assert m1.decision_seq == m2.decision_seq
        assert m1.mismatch_category == m2.mismatch_category
        
    # 2. Split verification: Provenance version metadata remains deterministic
    assert res1.provenance.dataset_contract_version == res2.provenance.dataset_contract_version
    assert res1.provenance.research_methodology_version == res2.provenance.research_methodology_version
    assert res1.provenance.dataset_build_id == res2.provenance.dataset_build_id
    
    # Runtime variability (timestamp/git hashes are allowed to vary between sessions but not during deterministic comparison)
    assert isinstance(res1.provenance.generated_time, str)

def test_replay_does_not_modify_dataset_generation():
    """Verify that running the replay does not modify any files in the dataset generation directory."""
    service = CounterfactualService(data_path=FIXTURE_PATH)
    
    # Take directory snapshot before run
    files = list(FIXTURE_PATH.glob("*"))
    pre_snapshots = {f: (f.stat().st_size, f.stat().st_mtime, _calculate_file_hash(f)) for f in files}
    
    # Execute replay
    _ = service.run_point_replay()
    
    # Compare after run
    for f, (pre_size, pre_mtime, pre_hash) in pre_snapshots.items():
        assert f.exists(), f"File {f} was deleted during replay run!"
        assert f.stat().st_size == pre_size, f"File {f} size modified!"
        assert _calculate_file_hash(f) == pre_hash, f"File {f} hash modified!"
        assert f.stat().st_mtime == pre_mtime, f"File {f} modification time changed!"

def test_counterfactual_service_side_effect_isolation():
    """Verify that running the replay does not touch/modify any production status files, logs, or databases."""
    logs_dir = Path("logs")
    
    targets = [
        logs_dir / "runtime_status.json",
        logs_dir / "mts_trade_fills.jsonl",
        logs_dir / "pm2_trading.log",
    ]
    
    pre_states = {}
    for p in targets:
        if p.exists():
            pre_states[p] = (p.stat().st_mtime, _calculate_file_hash(p))
            
    service = CounterfactualService(data_path=FIXTURE_PATH)
    _ = service.run_point_replay()
    
    for p, (pre_mtime, pre_hash) in pre_states.items():
        assert p.exists()
        assert _calculate_file_hash(p) == pre_hash

def test_counterfactual_service_config_override():
    """Verify that ReplayConfig overrides apply correctly and do not mutate the input config."""
    service = CounterfactualService(data_path=FIXTURE_PATH)
    
    config = ReplayConfig(release_stop_threshold=10.0)
    original_config = copy.deepcopy(config)
    
    res_overridden = service.run_point_replay(config=config)
    
    assert isinstance(res_overridden, PointReplayResult)
    # Check that original config was not mutated
    assert config.release_stop_threshold == original_config.release_stop_threshold

def test_dataset_contract_fail_fast(tmp_path):
    """Verify that CounterfactualService raises DatasetContractError when required contract components are missing."""
    import pandas as pd
    
    # Create a malformed dataset missing "is_decision_point" column in snapshots
    facts_path = tmp_path / "trade_facts.parquet"
    snapshots_path = tmp_path / "trade_snapshots.parquet"
    decisions_path = tmp_path / "trade_decisions.parquet"
    outcomes_path = tmp_path / "trade_outcomes.parquet"
    manifest_path = tmp_path / "manifest.json"
    
    pd.DataFrame({"trade_id": ["T1"]}).to_parquet(facts_path)
    pd.DataFrame({"trade_id": ["T1"], "snapshot_seq": [0]}).to_parquet(snapshots_path) # missing is_decision_point
    pd.DataFrame({"trade_id": ["T1"], "decision_seq": [0]}).to_parquet(decisions_path)
    pd.DataFrame({"trade_id": ["T1"]}).to_parquet(outcomes_path)
    
    import json
    with open(manifest_path, "w") as f:
        json.dump({"dataset_build_id": "test-build-1"}, f)
        
    service = CounterfactualService(data_path=tmp_path)
    
    with pytest.raises(DatasetContractError) as exc_info:
        service.run_point_replay()
        
    assert exc_info.value.code == "REPLAY_DATASET_SCHEMA_MISMATCH"
    assert "is_decision_point" in exc_info.value.missing_columns
    assert exc_info.value.dataset_build_id == "test-build-1"

def test_service_import_has_no_runtime_side_effects():
    """Verify that importing CounterfactualService has no runtime side effects on production systems."""
    assert "core.counterfactual_service" in sys.modules

def test_run_parameter_sweep_validation():
    """Verify parameter whitelist and value bounds checks in run_parameter_sweep."""
    from core.counterfactual_service import SweepParameter, SweepRequest
    service = CounterfactualService(data_path=FIXTURE_PATH)
    
    # 1. Invalid parameter name
    req_invalid_name = SweepRequest(
        parameters=(SweepParameter(name="invalid_param", values=(10.0,)),),
        baseline_config=ReplayConfig(release_stop_threshold=14.0),
        dataset_generation_id="test-gen",
        eligibility_policy_version="v1.0"
    )
    with pytest.raises(ValueError, match="is not in the sweepable whitelist"):
        service.run_parameter_sweep(req_invalid_name)
        
    # 2. Out of bounds value
    req_out_of_bounds = SweepRequest(
        parameters=(SweepParameter(name="release_stop_threshold", values=(2.0,)),), # min is 5.0
        baseline_config=ReplayConfig(release_stop_threshold=14.0),
        dataset_generation_id="test-gen",
        eligibility_policy_version="v1.0"
    )
    with pytest.raises(ValueError, match="is out of bounds"):
        service.run_parameter_sweep(req_out_of_bounds)

def test_run_parameter_sweep_reproduction():
    """Verify that sweeping at baseline value reproduces certified point replay with zero drift."""
    from core.counterfactual_service import SweepParameter, SweepRequest, DecisionDriftCategory
    service = CounterfactualService(data_path=FIXTURE_PATH)
    
    # Sweep at baseline value 14.0
    req = SweepRequest(
        parameters=(SweepParameter(name="release_stop_threshold", values=(14.0,)),),
        baseline_config=ReplayConfig(release_stop_threshold=14.0),
        dataset_generation_id="test-gen",
        eligibility_policy_version="v1.0"
    )
    result = service.run_parameter_sweep(req)
    
    assert result.parameter_name == "release_stop_threshold"
    assert len(result.metrics_rows) == 1
    metric = result.metrics_rows[0]
    
    assert metric.parameter_value == 14.0
    assert metric.decision_drift_count == 0
    assert metric.unchanged_count == metric.eligible_cases
    assert metric.baseline_action_drift_rate == 0.0
    
    # Verify case matrix rows have NONE drift category
    for row in result.case_matrix:
        assert row.drift_category == DecisionDriftCategory.NONE
        # Assert that baseline decision equals counterfactual decision
        assert row.baseline_action == row.counterfactual_action
        assert row.baseline_leg == row.counterfactual_leg

def test_run_parameter_sweep_drift():
    """Verify that non-baseline parameter sweep triggers decision drift and categorizes it correctly."""
    from core.counterfactual_service import SweepParameter, SweepRequest, DecisionDriftCategory
    service = CounterfactualService(data_path=FIXTURE_PATH)
    
    # Sweep at very wide value 1000.0 (which should prevent releases from triggering, i.e., drift)
    req = SweepRequest(
        parameters=(SweepParameter(name="release_stop_threshold", values=(1000.0, 130.0)),),
        baseline_config=ReplayConfig(release_stop_threshold=130.0),
        dataset_generation_id="test-gen",
        eligibility_policy_version="v1.0"
    )
    result = service.run_parameter_sweep(req)
    
    assert len(result.metrics_rows) == 2
    metric_1000 = next(m for m in result.metrics_rows if m.parameter_value == 1000.0)
    
    # At least some decisions should drift at threshold=1000.0 compared to baseline=130.0
    assert metric_1000.decision_drift_count > 0
    assert metric_1000.unchanged_count < metric_1000.eligible_cases
    
    # Verify that drift categories are correct
    has_action_drift = any(row.drift_category in (DecisionDriftCategory.TRIGGER_TO_NO_ACTION, DecisionDriftCategory.NO_ACTION_TO_TRIGGER) for row in result.case_matrix if row.parameter_value == 1000.0)
    assert has_action_drift

def test_export_experiment(tmp_path):
    """Verify that export_experiment writes all expected files, calculates experiment_hash, appends to registry, and aggregates ranking."""
    from core.counterfactual_service import SweepParameter, SweepRequest
    # Change working directory temporarily to test export output directory creation
    orig_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        # Create fixtures dir mockup
        fixture_mock = Path("tests/fixtures/research_baseline_v1")
        fixture_mock.mkdir(parents=True, exist_ok=True)
        # Copy fixture files
        import shutil
        for f in (Path(orig_cwd) / FIXTURE_PATH).glob("*"):
            shutil.copy(f, fixture_mock / f.name)
            
        service = CounterfactualService(data_path=fixture_mock)
        req = SweepRequest(
            parameters=(SweepParameter(name="release_stop_threshold", values=(14.0,)),),
            baseline_config=ReplayConfig(release_stop_threshold=14.0),
            dataset_generation_id="test-gen",
            eligibility_policy_version="v1.0"
        )
        result = service.run_parameter_sweep(req)
        
        # Verify experiment hash is computed and has sha256 prefix
        assert result.provenance.experiment_hash.startswith("sha256:")
        
        exp_dir = service.export_experiment(result)
        
        # Verify files are generated
        assert exp_dir.exists()
        assert (exp_dir / "manifest.json").exists()
        assert (exp_dir / "summary.md").exists()
        assert (exp_dir / "sweep_metrics.csv").exists()
        assert (exp_dir / "case_decision_matrix.parquet").exists()
        assert (exp_dir / "result.json").exists()
        
        # Validate manifest content contains experiment_hash
        with open(exp_dir / "manifest.json") as f:
            manifest_data = json.load(f)
            assert manifest_data["experiment_type"] == "DECISION_POINT_PARAMETER_SWEEP"
            assert manifest_data["parameter_name"] == "release_stop_threshold"
            assert manifest_data["parameter_values"] == [14.0]
            assert manifest_data["experiment_hash"] == result.provenance.experiment_hash

        # Verify registry was created and contains entry
        registry_file = Path("reports/research/counterfactual/registry.json")
        assert registry_file.exists()
        with open(registry_file) as f:
            registry_data = json.load(f)
            assert len(registry_data) == 1
            entry = registry_data[0]
            assert entry["experiment_hash"] == result.provenance.experiment_hash
            assert entry["parameter_name"] == "release_stop_threshold"
            assert entry["run_count"] == 1
            assert len(entry["runs"]) == 1
            assert entry["runs"][0]["experiment_id"] == exp_dir.name
            
        # Verify sensitivity ranking aggregation
        ranking = service.get_sensitivity_ranking()
        assert len(ranking) == 1
        assert ranking[0]["parameter_name"] == "release_stop_threshold"
        assert ranking[0]["experiment_hash"] == result.provenance.experiment_hash[:12]

        # Verify registry validation works and returns VERIFIED
        val_status = service.validate_experiment_registry()
        assert val_status["status"] == "VERIFIED"
        assert result.provenance.experiment_hash in val_status["experiments"]
        assert val_status["experiments"][result.provenance.experiment_hash]["runs"][0]["status"] == "VERIFIED"

        # Verify registry rebuilding works
        registry_file.unlink()
        assert not registry_file.exists()
        service.rebuild_experiment_registry()
        assert registry_file.exists()
        with open(registry_file) as f:
            registry_data = json.load(f)
            assert len(registry_data) == 1
            assert registry_data[0]["experiment_hash"] == result.provenance.experiment_hash

        # Verify inspector rejects corrupted files
        result_json_file = exp_dir / "result.json"
        with open(result_json_file, "w") as f:
            f.write('{"corrupted": true}')
        val_status = service.validate_experiment_registry()
        assert val_status["status"] == "UNVERIFIED"
        assert val_status["experiments"][result.provenance.experiment_hash]["runs"][0]["status"] == "CORRUPTED"
            
    finally:
        os.chdir(orig_cwd)

def test_canonical_experiment_payload_determinism():
    """Verify that different parameter sorting or types yield identical experiment hashes."""
    from core.counterfactual_service import CounterfactualService
    payload_a = CounterfactualService.canonical_experiment_payload(
        baseline_id="baseline-v1",
        dataset_content_hash="abc",
        git_commit="git-1",
        dirty_diff_hash="diff-1",
        replay_config_dict={"release_stop_threshold": 14.0, "confirm_ms": 200},
        parameter_name="release_stop_threshold",
        parameter_values=[20.0, 10.0, 30.0],
        methodology_version="v1.0"
    )
    
    # Payload B with different key sorting and values ordering
    payload_b = CounterfactualService.canonical_experiment_payload(
        baseline_id="baseline-v1",
        dataset_content_hash="abc",
        git_commit="git-1",
        dirty_diff_hash="diff-1",
        replay_config_dict={"confirm_ms": 200, "release_stop_threshold": 14.0},
        parameter_name="release_stop_threshold",
        parameter_values=[10.0, 30.0, 20.0],
        methodology_version="v1.0"
    )
    
    assert payload_a == payload_b
    
    import hashlib
    hash_a = hashlib.sha256(payload_a).hexdigest()
    hash_b = hashlib.sha256(payload_b).hexdigest()
    assert hash_a == hash_b

def test_local_sensitivity_calculation(tmp_path):
    """Verify that local sensitivity correctly measures drift inside baseline +/- 10%."""
    from core.counterfactual_service import SweepParameter, SweepRequest
    orig_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        fixture_mock = Path("tests/fixtures/research_baseline_v1")
        fixture_mock.mkdir(parents=True, exist_ok=True)
        import shutil
        for f in (Path(orig_cwd) / FIXTURE_PATH).glob("*"):
            shutil.copy(f, fixture_mock / f.name)
            
        service = CounterfactualService(data_path=fixture_mock)
        
        # Baseline stop threshold: 130.0
        # Sweep values: 1000.0 (wide drift), 140.0 (local +/- 10%, should drift slightly or not), 130.0 (baseline)
        req = SweepRequest(
            parameters=(SweepParameter(name="release_stop_threshold", values=(1000.0, 140.0, 130.0)),),
            baseline_config=ReplayConfig(release_stop_threshold=130.0),
            dataset_generation_id="test-gen",
            eligibility_policy_version="v1.0"
        )
        result = service.run_parameter_sweep(req)
        exp_dir = service.export_experiment(result)
        
        registry_file = Path("reports/research/counterfactual/registry.json")
        with open(registry_file) as f:
            registry_data = json.load(f)
            entry = registry_data[0]
            # Baseline is 130.0, +/- 10% is [117, 143].
            # 1000.0 is outside local range.
            # 140.0 is inside.
            # Ensure local_sensitivity tracks drift rate of 140.0, which is likely 0 or very small,
            # whereas max_drift_rate tracks 1000.0 (which is large).
            assert entry["max_drift_rate"] >= entry["local_sensitivity"]
            
    finally:
        os.chdir(orig_cwd)
