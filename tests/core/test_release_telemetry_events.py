"""Release-condition telemetry: the MTS release evaluation must be observable
even when the local fills ledger is empty (broker position without local fills).

Events locked by this suite:
- RELEASE_CONDITION_MET           — strategy signals a release/exit, before submit
- RELEASE_EVAL_SKIP_NO_LOCAL_POSITION — broker holds TMF legs but the strategy
                                        sees no local position (fills gap)
- FILL_REJECTED_REMAINING         — apply_deal_fill rejected (remaining=0)

Fail-closed rules: when the local position cannot be resolved (no strategy
reference, no persisted state), NO divergence event is emitted.
"""
import json
import time
from types import SimpleNamespace

import pytest

from strategies.futures.monitor import FuturesMonitor, _mts_position_state_path


def _make_monitor():
    events = []

    class _Mon(SimpleNamespace):
        def _append_mts_event(self, event_type, **kwargs):
            events.append({"event": event_type, **kwargs})

    mon = _Mon()
    mon.events = events
    mon._release_eval_skip_last_emit = 0.0
    mon._fill_rejected_last_emit = 0.0
    # Bind the real monitor methods onto the stub so the emission logic
    # under test is exactly the production code.
    mon._emit_release_telemetry = FuturesMonitor._emit_release_telemetry.__get__(mon, type(mon))
    mon._emit_release_eval_skip_no_local_position = (
        FuturesMonitor._emit_release_eval_skip_no_local_position.__get__(mon, type(mon)))
    mon._emit_fill_rejected = FuturesMonitor._emit_fill_rejected.__get__(mon, type(mon))
    mon._resolve_mts_local_position_qty = (
        FuturesMonitor._resolve_mts_local_position_qty.__get__(mon, type(mon)))
    return mon


def _make_strategy(**attrs):
    return SimpleNamespace(**attrs)


def _make_snapshot_with_legs():
    return {
        "source": "live_broker",
        "positions": [
            {"code": "TMFH6", "direction": "Action.Sell", "quantity": 1, "avg_cost": 46156.0},
            {"code": "TMFI6", "direction": "Action.Buy", "quantity": 1, "avg_cost": 46329.0},
        ],
        "open_orders": [],
    }


def _write_state_file(payload):
    _p = _mts_position_state_path()
    _p.parent.mkdir(parents=True, exist_ok=True)
    with open(_p, "w", encoding="utf-8") as _f:
        json.dump(payload, _f)


@pytest.fixture(autouse=True)
def _clean_state_file():
    yield
    _p = _mts_position_state_path()
    try:
        _p.unlink()
    except OSError:
        pass


# ── RELEASE_CONDITION_MET ──

def test_release_condition_met_carries_production_trade_id():
    mon = _make_monitor()
    strat = _make_strategy(_trade_id="t-1")  # production attr: strategy._trade_id
    mon._emit_release_telemetry("RELEASE_NEAR", strat, {})
    assert len(mon.events) == 1
    assert mon.events[0]["event"] == "RELEASE_CONDITION_MET"
    assert mon.events[0]["signal"] == "RELEASE_NEAR"
    assert mon.events[0]["trade_id"] == "t-1"


def test_release_condition_met_falls_back_to_plain_trade_id():
    mon = _make_monitor()
    mon._emit_release_telemetry("RELEASE_NEAR", _make_strategy(trade_id="t-2"), {})
    assert mon.events and mon.events[0]["trade_id"] == "t-2"


def test_release_condition_met_emits_for_combined_exit():
    mon = _make_monitor()
    mon._emit_release_telemetry("COMBINED_EXIT_NEAR", _make_strategy(), {})
    assert mon.events and mon.events[0]["event"] == "RELEASE_CONDITION_MET"


def test_entry_signal_emits_no_release_event():
    mon = _make_monitor()
    mon._emit_release_telemetry("SELL_NEAR_BUY_FAR", _make_strategy(), {})
    assert mon.events == []


# ── RELEASE_EVAL_SKIP_NO_LOCAL_POSITION ──

def test_divergent_broker_legs_without_local_position_emits_event():
    mon = _make_monitor()
    mon._mts_strategy = _make_strategy(_near_qty=0, _far_qty=0)
    mon._emit_release_eval_skip_no_local_position(_make_snapshot_with_legs())
    assert len(mon.events) == 1
    assert mon.events[0]["event"] == "RELEASE_EVAL_SKIP_NO_LOCAL_POSITION"
    assert "TMFH6" in mon.events[0]["broker_legs"][0]


def test_divergence_resolves_strategy_from_registry():
    mon = _make_monitor()  # no _mts_strategy attr
    mon._strategies = {"mts": _make_strategy(_near_qty=0, _far_qty=0)}
    mon._emit_release_eval_skip_no_local_position(_make_snapshot_with_legs())
    assert len(mon.events) == 1
    assert mon.events[0]["event"] == "RELEASE_EVAL_SKIP_NO_LOCAL_POSITION"


def test_local_position_present_emits_no_skip_event():
    mon = _make_monitor()
    mon._mts_strategy = _make_strategy(_near_qty=1, _far_qty=1)
    mon._emit_release_eval_skip_no_local_position(_make_snapshot_with_legs())
    assert mon.events == []


def test_no_broker_legs_emits_no_skip_event():
    mon = _make_monitor()
    mon._mts_strategy = _make_strategy(_near_qty=0, _far_qty=0)
    mon._emit_release_eval_skip_no_local_position({"positions": [], "source": "live_broker"})
    assert mon.events == []


def test_divergence_resolves_strategy_from_registry_get():
    """Production path: self._registry.get('tmf_spread') is the real accessor."""
    mon = _make_monitor()
    mon._registry = SimpleNamespace(
        get=lambda name: _make_strategy(_near_qty=0, _far_qty=0)
        if name == "tmf_spread" else None)
    mon._emit_release_eval_skip_no_local_position(_make_snapshot_with_legs())
    assert len(mon.events) == 1
    assert mon.events[0]["event"] == "RELEASE_EVAL_SKIP_NO_LOCAL_POSITION"


def test_registry_strategy_with_position_emits_no_skip():
    mon = _make_monitor()
    mon._registry = SimpleNamespace(
        get=lambda name: _make_strategy(_near_qty=1, _far_qty=1)
        if name == "tmf_spread" else None)
    mon._emit_release_eval_skip_no_local_position(_make_snapshot_with_legs())
    assert mon.events == []


def test_unknown_local_position_emits_no_skip_event():
    """Fail-closed: no strategy reference and no state file -> no event."""
    mon = _make_monitor()
    mon._emit_release_eval_skip_no_local_position(_make_snapshot_with_legs())
    assert mon.events == []


def test_state_file_position_present_emits_no_skip_event():
    _write_state_file({"has_position": True, "trade_id": "t-1"})
    mon = _make_monitor()
    mon._emit_release_eval_skip_no_local_position(_make_snapshot_with_legs())
    assert mon.events == []


def test_state_file_flat_confirms_divergence():
    _write_state_file({"has_position": False})
    mon = _make_monitor()
    mon._emit_release_eval_skip_no_local_position(_make_snapshot_with_legs())
    assert len(mon.events) == 1
    assert mon.events[0]["event"] == "RELEASE_EVAL_SKIP_NO_LOCAL_POSITION"


def test_divergence_emission_rate_limited_to_30s():
    mon = _make_monitor()
    mon._mts_strategy = _make_strategy(_near_qty=0, _far_qty=0)
    mon._release_eval_skip_last_emit = time.monotonic() - 5.0  # within window
    mon._emit_release_eval_skip_no_local_position(_make_snapshot_with_legs())
    assert mon.events == []


# ── FILL_REJECTED_REMAINING ──

def test_fill_rejected_emits_event():
    mon = _make_monitor()
    mon._emit_fill_rejected("ORD-X", "Fill qty 1 exceeds remaining 0 for ORD-X")
    assert len(mon.events) == 1
    assert mon.events[0]["event"] == "FILL_REJECTED_REMAINING"
    assert mon.events[0]["order_id"] == "ORD-X"


def test_fill_rejected_rate_limited_to_30s():
    mon = _make_monitor()
    mon._fill_rejected_last_emit = time.monotonic() - 5.0
    mon._emit_fill_rejected("ORD-X", "reason")
    assert mon.events == []
