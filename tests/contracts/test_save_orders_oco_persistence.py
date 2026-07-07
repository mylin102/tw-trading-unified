"""
Regression tests for _save_orders_file_wrapper OCO persistence.

1. release_group.SUBMITTED + order_mgr empty + strategy lifecycle missing
   → state-file fallback adds near/far OCO, returns {near_id, far_id}

2. Fallback exception
   → return set(), no _mts_release_orders_flushed set, warning logged
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import strategies.futures.monitor as monitor_module


@pytest.fixture
def fixture_state_file(tmp_path):
    """Isolated state file inside tmp_path — never touches /tmp/."""
    return tmp_path / "mts_position_state.json"


@pytest.fixture
def fixture_monitor(tmp_path, monkeypatch):
    """Minimal FuturesMonitor: empty order_mgr, no strategy lifecycle."""
    from strategies.futures.monitor import FuturesMonitor

    exports_dir = tmp_path / "exports" / "trades"
    exports_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    # 2026-07-07 Gemini CLI / Hermes Agent: Isolate state path via environment variable to prevent test pollution
    monkeypatch.setenv("MTS_STATE_PATH", str(tmp_path / "mts_position_state.json"))

    m = FuturesMonitor.__new__(FuturesMonitor)
    object.__setattr__(m, "order_mgr", SimpleNamespace(
        get_pending=lambda: [],
        get_completed=lambda: [],
        completed=[],
    ))
    _registry = SimpleNamespace()
    _registry.get = MagicMock(return_value=None)
    object.__setattr__(m, "_registry", _registry)
    object.__setattr__(m, "ticker", "TMF")
    object.__setattr__(m, "market_data", {})
    object.__setattr__(m, "trader", SimpleNamespace(position=0, entry_price=0.0))
    return m


def test_regression_1_state_file_fallback_adds_oco(
    fixture_monitor, fixture_state_file, tmp_path, monkeypatch
):
    """release_group.SUBMITTED + empty order_mgr + no strategy lifecycle
    → state-file fallback persists near/far OCO, returns {near_id, far_id}."""
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
    fixture_state_file.write_text(json.dumps(state))
    monkeypatch.setattr(
        monitor_module, "MTS_POSITION_STATE_PATH", fixture_state_file
    )

    persisted = fixture_monitor._save_orders_file_wrapper()

    assert "ORD-20260707-000005" in persisted, f"near OCO missing, got {persisted}"
    assert "ORD-20260707-000006" in persisted, f"far OCO missing, got {persisted}"
    assert len(persisted) == 2, f"expected 2 OCO IDs, got {persisted}"

    from core.date_utils import get_session_date_str
    session_date = get_session_date_str()
    orders_file = tmp_path / "exports" / "trades" / f"TMF_{session_date}_orders.json"
    assert orders_file.exists(), "orders file not written"
    orders = json.loads(orders_file.read_text())
    oco_orders = [o for o in orders if o.get("strategy") == "MTS_RELEASE_OCO"]
    oco_ids = {o["order_id"] for o in oco_orders}
    assert oco_ids == {"ORD-20260707-000005", "ORD-20260707-000006"}, \
        f"OCO orders mismatch in file: {oco_ids}"


def test_regression_2_fallback_exception_returns_empty(
    fixture_monitor, fixture_state_file, tmp_path, monkeypatch
):
    """Corrupt state file → return set(), no crash, warning emitted."""
    fixture_state_file.write_text("NOT VALID JSON {{{")
    monkeypatch.setattr(
        monitor_module, "MTS_POSITION_STATE_PATH", fixture_state_file
    )

    with patch("strategies.futures.monitor.console.print") as mock_print:
        persisted = fixture_monitor._save_orders_file_wrapper()

    assert persisted == set(), f"expected empty set on exception, got {persisted}"

    warning_calls = [
        str(call) for call in mock_print.call_args_list
        if "state-file fallback failed" in str(call)
    ]
    assert len(warning_calls) > 0, "expected warning log for fallback failure"

    from core.date_utils import get_session_date_str
    session_date = get_session_date_str()
    orders_file = tmp_path / "exports" / "trades" / f"TMF_{session_date}_orders.json"
    assert orders_file.exists(), "orders file should still be written"
    orders = json.loads(orders_file.read_text())
    assert isinstance(orders, list), "orders file should be a valid JSON array"
