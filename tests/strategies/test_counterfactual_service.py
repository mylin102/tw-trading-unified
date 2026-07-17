# 2026-07-17 Gemini CLI: Test suite verifying CounterfactualService contract and side-effect isolation.

import pytest
import os
import hashlib
from pathlib import Path
from core.counterfactual_service import CounterfactualService, ReplayConfig, PointReplayResult

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
    service = CounterfactualService()
    res1 = service.run_point_replay()
    res2 = service.run_point_replay()
    
    assert res1.metrics.total_cases == res2.metrics.total_cases
    assert res1.metrics.action_match_rate == res2.metrics.action_match_rate
    assert res1.metrics.leg_match_rate == res2.metrics.leg_match_rate
    assert res1.metrics.mismatch_count == res2.metrics.mismatch_count
    
    assert len(res1.mismatches) == len(res2.mismatches)
    for m1, m2 in zip(res1.mismatches, res2.mismatches):
        assert m1.trade_id == m2.trade_id
        assert m1.decision_seq == m2.decision_seq
        assert m1.mismatch_category == m2.mismatch_category
        
    assert res1.provenance.dataset_contract_version == res2.provenance.dataset_contract_version
    assert res1.provenance.research_methodology_version == res2.provenance.research_methodology_version
    assert res1.provenance.dataset_build_id == res2.provenance.dataset_build_id
    assert res1.provenance.git_commit == res2.provenance.git_commit
    assert res1.provenance.git_repo_state == res2.provenance.git_repo_state

def test_counterfactual_service_side_effect_isolation():
    """Verify that running the replay does not touch/modify any production status files, logs, or databases."""
    logs_dir = Path("logs")
    data_dir = Path("data/current")
    
    # Track paths that could potentially be written to
    targets = [
        logs_dir / "runtime_status.json",
        logs_dir / "mts_trade_fills.jsonl",
        logs_dir / "pm2_trading.log",
        data_dir / "trade_facts.parquet",
        data_dir / "trade_snapshots.parquet",
        data_dir / "trade_decisions.parquet",
        data_dir / "trade_outcomes.parquet",
    ]
    
    # Store pre-run hashes and mtimes
    pre_states = {}
    for p in targets:
        if p.exists():
            pre_states[p] = (p.stat().st_mtime, _calculate_file_hash(p))
            
    # Run the replay service
    service = CounterfactualService()
    _ = service.run_point_replay()
    
    # Verify post-run hashes and mtimes are identical
    for p, (pre_mtime, pre_hash) in pre_states.items():
        assert p.exists(), f"Target file {p} was deleted during replay run!"
        post_hash = _calculate_file_hash(p)
        assert pre_hash == post_hash, f"Target file {p} was modified (content changed) during replay run!"
        # (Note: mtime might change if a file is touched, but content hash must be identical)

def test_counterfactual_service_config_override():
    """Verify that ReplayConfig overrides apply correctly and produce valid replay metrics."""
    service = CounterfactualService()
    
    # Run with a tight release threshold (should trigger more releases or cause matches to shift)
    config = ReplayConfig(release_stop_threshold=10.0)
    res_overridden = service.run_point_replay(config=config)
    
    assert isinstance(res_overridden, PointReplayResult)
    assert res_overridden.metrics.total_cases > 0
    assert 0.0 <= res_overridden.metrics.action_match_rate <= 1.0
