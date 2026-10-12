"""2026-10-08 Codex: runtime parameters must not move either release HEAD."""
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.core.test_mts_mode_data_isolation import isolated_runtime


def _monitor(rt, monkeypatch):
    from strategies.futures.monitor import FuturesMonitor
    path = rt.parent / "config/futures.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ticker: TMF\nlive_trading: false\nmonitoring:\n  use_order_manager: true\n")
    return FuturesMonitor(api=SimpleNamespace(), config_path=str(path), dry_run=True)


def profile(rt, *, live=False):
    path = rt / "config/futures.paper.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ticker: TMF\nlive_trading: " + str(live).lower() +
                    "\nmonitoring:\n  use_order_manager: true\n"
                    "mts:\n  params:\n    max_spread_width: 4\n")
    return path


def dashboard_truth(base):
    # Execute the real loader/classifier without importing Streamlit or broker SDK.
    source = Path(__file__).resolve().parents[2] / "ui/dashboard.py"
    nodes = [n for n in ast.parse(source.read_text()).body
             if isinstance(n, ast.FunctionDef) and n.name in
             {"load_active_runtime_truth", "summarize_execution_context"}]
    ns = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
    return ns["load_active_runtime_truth"](base)


def test_actual_monitor_loads_shared_profile(isolated_runtime, monkeypatch):
    path = profile(isolated_runtime)
    mon = _monitor(isolated_runtime, monkeypatch)
    assert Path(mon.config_path) == path
    assert mon.cfg["mts"]["params"]["max_spread_width"] == 4


def test_actual_dashboard_ignores_other_release_baseline(isolated_runtime, tmp_path):
    path = profile(isolated_runtime)
    base = tmp_path / "dashboard-release"
    (base / "config").mkdir(parents=True)
    (base / "config/futures.yaml").write_text("live_trading: false\n")
    (isolated_runtime / "execution_context.json").write_text(json.dumps({
        "requested_mode": "paper", "effective_mode": "paper_active",
        "live_order_allowed": False, "config_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
    }))
    truth = dashboard_truth(base)
    assert truth["is_paper_runtime"] and truth["warning"] == ""


def test_actual_reload_publishes_shared_hash_without_restart(isolated_runtime, monkeypatch):
    path = profile(isolated_runtime)
    mon = _monitor(isolated_runtime, monkeypatch)
    mon.client = SimpleNamespace(_execution_context=mon._execution_context)
    path.write_text(path.read_text().replace("max_spread_width: 4", "max_spread_width: 3"))
    mon._config_mtime = 0
    mon._reload_config_if_changed()
    assert mon.cfg["mts"]["params"]["max_spread_width"] == 3
    assert mon._execution_context.config_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert mon._execution_context.live_order_allowed is False


def test_shared_profile_cannot_promote_live(isolated_runtime, monkeypatch):
    profile(isolated_runtime, live=True)
    with pytest.raises(ValueError, match="PAPER_PROFILE"):
        _monitor(isolated_runtime, monkeypatch)


def test_sealed_live_profile_never_redirects(isolated_runtime, tmp_path):
    from core.futures_profiles import resolve_futures_profile
    profile(isolated_runtime)
    live = tmp_path / "config/futures_live.yaml"
    assert resolve_futures_profile(live) == live


def test_atomic_profile_validation_keeps_previous_bytes(isolated_runtime):
    from core.futures_profiles import write_paper_profile
    path = profile(isolated_runtime)
    old = path.read_bytes()
    with pytest.raises(ValueError, match="PAPER_PROFILE"):
        write_paper_profile(path, b"live_trading: true\n", ticker="TMF")
    assert path.read_bytes() == old
