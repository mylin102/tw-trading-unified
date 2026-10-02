"""2026-10-02 Codex: actual PAPER reload must publish the applied config identity."""
import ast
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.core.test_mts_mode_data_isolation import isolated_runtime, _monitor


def _change_config(mon, *, live=False):
    path = Path(mon.config_path)
    path.write_text("ticker: TMF\nlive_trading: " + str(live).lower() +
                    "\ncooldown_bars: 17\nmonitoring:\n  use_order_manager: true\n")
    mon._config_mtime = 0
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_actual_paper_reload_publishes_applied_hash(isolated_runtime, monkeypatch):
    mon = _monitor(isolated_runtime, monkeypatch)
    prior = mon._execution_context
    mon.client = SimpleNamespace(_execution_context=prior)
    expected = _change_config(mon)
    assert expected != prior.config_hash
    mon._reload_config_if_changed()
    ctx = mon._execution_context
    assert mon.cooldown_bars == 17
    assert ctx.config_hash == expected
    assert mon.client._execution_context is ctx
    assert mon.order_mgr.execution_context is ctx
    assert ctx.requested_mode == "paper" and ctx.effective_mode == "paper_active"
    assert not ctx.live_order_allowed
    assert ctx.session_id == prior.session_id
    persisted = json.loads((isolated_runtime / "execution_context.json").read_text())
    assert persisted["config_hash"] == expected
    # Execute the actual dashboard classifier without starting Streamlit.
    source = Path(__file__).resolve().parents[2] / "ui/dashboard.py"
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == "summarize_execution_context")
    ns = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), ns)
    truth = ns["summarize_execution_context"](
        persisted, {"futures.yaml (Paper baseline)": expected})
    assert truth["is_paper_runtime"] and truth["warning"] == ""


@pytest.mark.parametrize("requested,effective,allowed,live_config", [
    ("live", "live_ready", True, True),
    ("paper", "paper_draining", False, False),
    ("paper", "paper_active", False, True),
])
def test_actual_reload_does_not_rebind_other_authority(
        isolated_runtime, monkeypatch, requested, effective, allowed, live_config):
    mon = _monitor(isolated_runtime, monkeypatch)
    mon._execution_context = replace(mon._execution_context, requested_mode=requested,
                                     effective_mode=effective, live_order_allowed=allowed)
    prior = mon._execution_context
    mon.client = SimpleNamespace(_execution_context=prior)
    mon.order_mgr.execution_context = prior
    _change_config(mon, live=live_config)
    mon._reload_config_if_changed()
    assert mon._execution_context is prior
    assert mon.client._execution_context is prior
    assert mon.order_mgr.execution_context is prior


def test_actual_reload_persist_failure_keeps_prior_hash(isolated_runtime, monkeypatch):
    mon = _monitor(isolated_runtime, monkeypatch)
    prior = mon._execution_context
    mon.client = SimpleNamespace(_execution_context=prior)
    path = isolated_runtime / "execution_context.json"
    original = path.read_bytes()
    _change_config(mon)
    from core import execution_context_state
    def fail_persist(*args, **kwargs):
        raise OSError("fixture write refusal")
    monkeypatch.setattr(execution_context_state, "persist_execution_context", fail_persist)
    mon._reload_config_if_changed()
    assert mon._execution_context is prior
    assert mon.client._execution_context is prior
    assert mon.order_mgr.execution_context is prior
    assert path.read_bytes() == original
    assert mon._config_mtime == 0  # failed publication must remain retryable
