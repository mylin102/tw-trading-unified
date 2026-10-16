"""Actual CLI/callsites: no synthetic dropped fields or copied production logic."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / 'scripts/validation/check_undefined_names.py'

@pytest.mark.parametrize('relative', [
    'strategies/futures/monitor.py',
    'strategies/options/live_options_squeeze_monitor.py',
    'strategies/options/options_engine/engine/broker_adapter.py',
    'core/attribution_dashboard.py', 'core/edge_model.py',
    'core/futures_strategy_router.py', 'core/mode_transition.py',
    'core/performance_provenance.py',
])
def test_real_module_cli_zero_undefined_names(relative):
    result = subprocess.run([sys.executable, '-B', str(CHECKER), str(ROOT/relative)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

def test_actual_strategies_core_tree_gate():
    result = subprocess.run([sys.executable, '-B', str(CHECKER), 'strategies', 'core'],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '0 undefined names; 0 scan errors' in result.stdout

def test_real_edge_ml_callsite_uses_model_instead_of_silent_fallback():
    import numpy as np
    from core.edge_model import EdgeModel
    model = EdgeModel.__new__(EdgeModel)
    vectors = []
    model._scaler = SimpleNamespace(transform=lambda vec: vectors.append(vec) or vec)
    model._ml_model = SimpleNamespace(predict_proba=lambda vec: np.array([[0.12, 0.88]]))
    assert model.compute_edge({'trend_strength': 0.9}) == 0.88
    assert vectors[0].shape == (1, 8)

def test_real_provenance_reads_current_runtime_session(tmp_path, monkeypatch):
    import core.runtime_paths as paths
    from core.performance_provenance import _current_runtime_session_id
    target = tmp_path/'execution_context.json'
    target.write_text('{"session_id":"isolated-current-session"}')
    monkeypatch.setattr(paths, 'runtime_path', lambda *a: str(target))
    assert _current_runtime_session_id() == 'isolated-current-session'
