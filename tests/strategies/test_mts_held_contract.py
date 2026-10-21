"""N1 (P0): MTS exits and settlement must act on the contracts the position
actually holds, not on the monitor's currently subscribed contracts.

Repro (2026-10-21, TMF settlement day): entry on TMFJ6/TMFK6; at 13:30 the
monitor rolled self.contract/self.far_contract to TMFK6/TMFL6; COMBINED_EXIT
was then written against TMFK6/TMFL6 and TMFJ6 was never closed.

Synthetic contracts only: no runtime CSV or broker data is read here.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategies.futures.monitor import FuturesMonitor
from strategies.plugins.futures.active.tmf_spread import (
    PositionLifecycle, PositionPhase,
)


def _contract(code, delivery_date):
    return SimpleNamespace(code=code, delivery_date=delivery_date)


def _strategy(has_position=True, near=None, far=None):
    s = MagicMock()
    s._has_position = has_position
    s._trade_id = "N1-TRADE"
    s._near_side = "SHORT"
    s._far_side = "LONG"
    s._near_qty = 1
    s._far_qty = 1
    s._lifecycle_oca = PositionLifecycle(phase=PositionPhase.SPREAD)
    s._held_near_contract = near
    s._held_far_contract = far
    return s


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Point position state and the event log at tmp files; never read runtime."""
    import strategies.futures.monitor as monitor_module

    monkeypatch.delenv("NODE_ENV", raising=False)
    monkeypatch.setenv("MTS_EVENT_LOG_PATH", str(tmp_path / "mts_spread_events.jsonl"))
    state_path = tmp_path / "mts_position_state.json"
    monkeypatch.setattr(monitor_module, "_mts_position_state_path", lambda: state_path)
    return SimpleNamespace(state_path=state_path, events_path=tmp_path / "mts_spread_events.jsonl")


@pytest.fixture
def monitor():
    """Monitor after the 13:30 rollover: subscribed contracts are the NEW pair."""
    m = FuturesMonitor.__new__(FuturesMonitor)
    m.live_trading = False
    m.dry_run = False
    m._registry = {}
    m.cfg = {"mts": {"strategy": "tmf_spread"}}
    m.contract = _contract("TMFK6", "2026/11/18")
    m.far_contract = _contract("TMFL6", "2026/12/16")
    m.order_mgr = MagicMock()
    m.order_mgr.active_orders = {}
    m.order_mgr.completed = []
    m.events = []
    m._append_mts_event = lambda name, **fields: m.events.append((name, fields))
    m._execution_context = None
    m._validate_exit_only_position = MagicMock(return_value=(True, None, ""))
    m._exit_only_authorization_ready = MagicMock(return_value=True)
    m._authorize_intent = MagicMock(return_value=(True, None, ""))
    m._mts_ledger_reconstructed_open_qty = MagicMock(return_value=None)
    m._leg_lock_enabled = MagicMock(return_value=False)
    m._collapse2_bind_exit_order = MagicMock()
    m._claimed_execution_keys = set()
    return m


def _held_pair():
    return (_contract("TMFJ6", "2026/10/21"), _contract("TMFK6", "2026/11/18"))


class TestHeldContractHelper:
    def test_held_contracts_returned_when_recorded(self, monitor):
        near, far = _held_pair()
        strategy = _strategy(near=near, far=far)

        assert monitor._held_leg_contracts(strategy) == (near, far)

    def test_held_contracts_none_when_not_recorded(self, monitor):
        strategy = _strategy(near=None, far=None)

        assert monitor._held_leg_contracts(strategy) is None


class TestExitUsesHeldContracts:
    def test_combined_exit_after_rollover_closes_held_legs(self, monitor):
        """Exit must go to TMFJ6/TMFK6 (held), never TMFK6/TMFL6 (subscribed)."""
        near, far = _held_pair()
        strategy = _strategy(near=near, far=far)
        signal = SimpleNamespace(action="EXIT", reason="COMBINED_EXIT")

        monitor._submit_mts_order_signal(signal, strategy, {}, datetime(2026, 10, 21, 13, 40))

        symbols = [c.kwargs["symbol"] for c in monitor.order_mgr.create_order.call_args_list]
        assert symbols == ["TMFJ6", "TMFK6"]

    def test_exit_without_held_contracts_fails_closed(self, monitor):
        """No recorded position contracts -> zero orders + POSITION_CONTRACT_UNAVAILABLE."""
        strategy = _strategy(near=None, far=None)
        signal = SimpleNamespace(action="EXIT", reason="COMBINED_EXIT")

        monitor._submit_mts_order_signal(signal, strategy, {}, datetime(2026, 10, 21, 13, 40))

        monitor.order_mgr.create_order.assert_not_called()
        assert [n for n, _ in monitor.events] == ["POSITION_CONTRACT_UNAVAILABLE"]


class TestContractConsistencyInvariant:
    def test_mismatch_blocks_and_records_both_codes(self, monitor):
        near, far = _held_pair()
        strategy = _strategy(near=near, far=far)

        blocked = monitor._exit_contract_mismatch(strategy, "TMFK6", "TMFL6")

        assert blocked is True
        assert monitor.events == [(
            "CONTRACT_MISMATCH_BLOCKED",
            {"trade_id": "N1-TRADE", "order_near": "TMFK6", "order_far": "TMFL6",
             "held_near": "TMFJ6", "held_far": "TMFK6"},
        )]

    def test_matching_codes_pass(self, monitor):
        near, far = _held_pair()
        strategy = _strategy(near=near, far=far)

        assert monitor._exit_contract_mismatch(strategy, "TMFJ6", "TMFK6") is False
        assert monitor.events == []


class TestSettlementGateHeldContract:
    """Gate keys off the held near leg's delivery date, window 13:25 <= t < 13:30."""

    TODAY = "2026/10/21"

    def _gate(self, monitor, now, near_delivery=TODAY, has_position=True):
        near = _contract("TMFJ6", near_delivery)
        far = _contract("TMFK6", "2026/11/18")
        strategy = _strategy(has_position=has_position, near=near, far=far)
        monitor._emergency_flatten_mts = MagicMock()
        result = monitor._mts_risk_gate_settlement(strategy, now=now)
        return result, monitor._emergency_flatten_mts

    def test_fires_at_1326_on_held_near_delivery_day(self, monitor):
        result, flatten = self._gate(monitor, datetime(2026, 10, 21, 13, 26))

        assert result is True
        flatten.assert_called_once()

    def test_does_not_fire_at_1331(self, monitor):
        result, flatten = self._gate(monitor, datetime(2026, 10, 21, 13, 31))

        assert result is False
        flatten.assert_not_called()

    def test_does_not_fire_before_1325(self, monitor):
        result, flatten = self._gate(monitor, datetime(2026, 10, 21, 13, 24))

        assert result is False
        flatten.assert_not_called()

    def test_does_not_fire_on_non_settlement_day(self, monitor):
        result, flatten = self._gate(monitor, datetime(2026, 10, 21, 13, 26), near_delivery="2026/10/20")

        assert result is False
        flatten.assert_not_called()

    def test_does_not_fire_when_flat(self, monitor):
        result, flatten = self._gate(monitor, datetime(2026, 10, 21, 13, 26), has_position=False)

        assert result is False
        flatten.assert_not_called()

    def test_fires_at_most_once_per_day(self, monitor):
        near = _contract("TMFJ6", self.TODAY)
        strategy = _strategy(near=near, far=_contract("TMFK6", "2026/11/18"))
        monitor._emergency_flatten_mts = MagicMock()

        monitor._mts_risk_gate_settlement(strategy, now=datetime(2026, 10, 21, 13, 26))
        monitor._mts_risk_gate_settlement(strategy, now=datetime(2026, 10, 21, 13, 27))

        monitor._emergency_flatten_mts.assert_called_once()


ENTRY_TRADE = "N1-TRADE"


def _write_state(path, **fields):
    import json
    path.write_text(json.dumps(fields), encoding="utf-8")


def _held_state(trade_id=ENTRY_TRADE):
    return dict(
        has_position=True, trade_id=trade_id,
        near_contract="TMFJ6", near_delivery_date="2026/10/21",
        far_contract="TMFK6", far_delivery_date="2026/11/18",
    )


def _restarted_strategy(trade_id=ENTRY_TRADE):
    """Strategy after a restart: in-memory held pair is gone, position is open."""
    s = _strategy(has_position=True, near=None, far=None)
    s._trade_id = trade_id
    return s


class TestRestartHydration:
    """Restart (PM2 / SESSION_INVALID exit 75) while holding: exits stay on the held pair."""

    def test_exit_after_restart_uses_state_file_held_pair(self, monitor, isolated_runtime):
        _write_state(isolated_runtime.state_path, **_held_state())
        strategy = _restarted_strategy()
        signal = SimpleNamespace(action="EXIT", reason="COMBINED_EXIT")

        monitor._submit_mts_order_signal(signal, strategy, {}, datetime(2026, 10, 21, 13, 40))

        symbols = [c.kwargs["symbol"] for c in monitor.order_mgr.create_order.call_args_list]
        assert symbols == ["TMFJ6", "TMFK6"]
        assert ("HELD_CONTRACTS_RESTORED", {
            "trade_id": ENTRY_TRADE, "source": "POSITION_STATE",
            "near_contract": "TMFJ6", "far_contract": "TMFK6"}) in monitor.events

    def test_exit_after_restart_falls_back_to_entry_audit(self, monitor, isolated_runtime):
        import json
        isolated_runtime.events_path.write_text(
            json.dumps({"event": "ENTRY_AUDIT", "trade_id": "OTHER",
                        "near_contract": "TMFH6", "far_contract": "TMFI6"}) + "\n"
            + json.dumps({"event": "ENTRY_AUDIT", "trade_id": ENTRY_TRADE,
                          "near_contract": "TMFJ6", "far_contract": "TMFK6",
                          "near_delivery_date": "2026/10/21",
                          "far_delivery_date": "2026/11/18"}) + "\n",
            encoding="utf-8")
        strategy = _restarted_strategy()
        signal = SimpleNamespace(action="EXIT", reason="COMBINED_EXIT")

        monitor._submit_mts_order_signal(signal, strategy, {}, datetime(2026, 10, 21, 13, 40))

        symbols = [c.kwargs["symbol"] for c in monitor.order_mgr.create_order.call_args_list]
        assert symbols == ["TMFJ6", "TMFK6"]
        assert ("HELD_CONTRACTS_RESTORED", {
            "trade_id": ENTRY_TRADE, "source": "ENTRY_AUDIT",
            "near_contract": "TMFJ6", "far_contract": "TMFK6"}) in monitor.events

    def test_state_for_other_trade_is_not_used(self, monitor, isolated_runtime):
        _write_state(isolated_runtime.state_path, **_held_state(trade_id="OTHER"))
        strategy = _restarted_strategy()
        signal = SimpleNamespace(action="EXIT", reason="COMBINED_EXIT")

        monitor._submit_mts_order_signal(signal, strategy, {}, datetime(2026, 10, 21, 13, 40))

        monitor.order_mgr.create_order.assert_not_called()
        assert [n for n, _ in monitor.events] == ["POSITION_CONTRACT_UNAVAILABLE"]

    def test_settlement_gate_after_restart_uses_restored_near_leg(self, monitor, isolated_runtime):
        _write_state(isolated_runtime.state_path, **_held_state())
        strategy = _restarted_strategy()
        monitor._emergency_flatten_mts = MagicMock()

        result = monitor._mts_risk_gate_settlement(strategy, now=datetime(2026, 10, 21, 13, 26))

        assert result is True
        monitor._emergency_flatten_mts.assert_called_once()


class TestStatePersistence:
    """Held pair written with the position survives later state writes; cleared when flat."""

    def test_held_pair_carried_forward_then_cleared_when_flat(self, isolated_runtime, monkeypatch):
        import json
        import strategies.plugins.futures.active.tmf_spread as tmf_spread

        monkeypatch.setattr(tmf_spread, "_get_state_file_path", lambda: str(isolated_runtime.state_path))
        tmf_spread._write_mts_state(
            has_position=True, action="SPREAD", reason="ENTRY", trade_id=ENTRY_TRADE,
            near_contract="TMFJ6", near_delivery_date="2026/10/21",
            far_contract="TMFK6", far_delivery_date="2026/11/18")
        tmf_spread._write_mts_state(has_position=True, action="SPREAD", reason="TICK", trade_id=ENTRY_TRADE)

        held = json.loads(isolated_runtime.state_path.read_text(encoding="utf-8"))
        assert (held["near_contract"], held["far_contract"]) == ("TMFJ6", "TMFK6")

        tmf_spread._write_mts_state(has_position=False, action="FLAT", reason="EXIT")
        flat = json.loads(isolated_runtime.state_path.read_text(encoding="utf-8"))
        assert flat["near_contract"] is None and flat["far_contract"] is None


class TestFillLegClassification:
    def test_near_leg_fill_after_rollover_is_classified_near(self, monitor):
        near, far = _held_pair()
        strategy = _strategy(near=near, far=far)

        assert monitor._classify_fill_leg(strategy, "TMFJ6") == "NEAR"
        assert monitor._classify_fill_leg(strategy, "TMFK6") == "FAR"
        assert monitor._classify_fill_leg(strategy, "TMFL6") is None

    def test_without_held_pair_falls_back_to_subscribed_codes(self, monitor):
        strategy = _strategy(near=None, far=None)
        monitor._restore_held_contracts = MagicMock(return_value=None)

        assert monitor._classify_fill_leg(strategy, "TMFK6") == "NEAR"
        assert monitor._classify_fill_leg(strategy, "TMFL6") == "FAR"
