"""
Contract test: MTS state file path injection via monkeypatch.

Verifies:
1. _mts_position_state_path() respects env override > constant
2. Test can monkeypatch constant without touching /tmp/
3. _save_orders_file_wrapper reads/writes via patched path
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import strategies.futures.monitor as monitor_module


@pytest.fixture(autouse=True)
def no_mts_state_env(monkeypatch):
    """Ensure MTS_STATE_PATH env var doesn't override the constant."""
    monkeypatch.delenv("MTS_STATE_PATH", raising=False)


def test_helper_no_env_uses_constant(monkeypatch, tmp_path):
    """Without MTS_STATE_PATH env, returns the constant."""
    assert monitor_module._mts_position_state_path() == Path(
        "/tmp/mts_position_state.json"
    )
    fixture = tmp_path / "custom.json"
    monkeypatch.setattr(monitor_module, "MTS_POSITION_STATE_PATH", fixture)
    assert monitor_module._mts_position_state_path() == fixture


def test_helper_env_override(monkeypatch, tmp_path):
    """With MTS_STATE_PATH set, env wins over constant."""
    override = tmp_path / "override.json"
    monkeypatch.setenv("MTS_STATE_PATH", str(override))
    assert monitor_module._mts_position_state_path() == override


def test_save_orders_fallback_uses_patched_path(tmp_path, monkeypatch):
    """State-file fallback in _save_orders_file_wrapper reads from patched path."""
    from strategies.futures.monitor import FuturesMonitor

    state_file = tmp_path / "isolated_state.json"
    state = {
        "lifecycle": {
            "phase": "SPREAD",
            "release_group": {
                "status": "SUBMITTED",
                "near_order_id": "ORD-20260707-000005",
                "far_order_id": "ORD-20260707-000006",
                "near_side": "buy",
                "far_side": "sell",
                "order_type": "MKP",
                "near_price": 46943.0,
                "far_price": 46750.0,
            },
        },
    }
    state_file.write_text(json.dumps(state))
    monkeypatch.setattr(monitor_module, "MTS_POSITION_STATE_PATH", state_file)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "exports" / "trades").mkdir(parents=True)

    m = FuturesMonitor.__new__(FuturesMonitor)
    object.__setattr__(m, "order_mgr", SimpleNamespace(
        get_pending=lambda: [],
        get_completed=lambda: [],
        completed=[],
    ))
    _reg = SimpleNamespace()
    _reg.get = MagicMock(return_value=None)
    object.__setattr__(m, "_registry", _reg)
    object.__setattr__(m, "ticker", "TMF")
    object.__setattr__(m, "market_data", {})
    object.__setattr__(m, "trader", SimpleNamespace(position=0, entry_price=0.0))

    persisted = m._save_orders_file_wrapper()

    assert "ORD-20260707-000005" in persisted
    assert "ORD-20260707-000006" in persisted

    from core.date_utils import get_session_date_str
    session_date = get_session_date_str()
    orders_file = tmp_path / "exports" / "trades" / f"TMF_{session_date}_orders.json"
    assert orders_file.exists()
    orders = json.loads(orders_file.read_text())
    oco_ids = {o["order_id"] for o in orders if o.get("strategy") == "MTS_RELEASE_OCO"}
    assert oco_ids == {"ORD-20260707-000005", "ORD-20260707-000006"}

    live_path = Path("/tmp/mts_position_state.json")
    assert not live_path.exists() or json.loads(
        live_path.read_text()
    ) != state, "live /tmp/ should not have test data"
