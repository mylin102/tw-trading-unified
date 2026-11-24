from datetime import datetime
from types import ModuleType
from unittest.mock import MagicMock
import sys

# Importing the monitor normally creates artifacts/replay under the repository.
# Stub its unused replay singleton so this telemetry test remains filesystem-isolated.
if "core.replay_engine" not in sys.modules:
    _replay_stub = ModuleType("core.replay_engine")
    _replay_stub.replay_engine = MagicMock()
    sys.modules["core.replay_engine"] = _replay_stub

from core.signal import Signal
from core.strategy_context import MarketData, PositionView, StrategyContext
from strategies.futures.monitor import FuturesMonitor
from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    Leg, PositionPhase, ReleaseGroupStatus,
)
from strategies.plugins.futures.active.tmf_spread import RecoveryState, TMFSpread
from tests.strategies.test_ilb_policy_j_precedence import _build_strategy, _make_bar
from tests.strategies.test_policy_j_combined_exit_execution import (
    DummyStrategy, make_mock_monitor,
)


BRACKET_STOP = "INDEPENDENT_LEG_BRACKET_STOP_LOSS"
BRACKET_TP = "INDEPENDENT_LEG_BRACKET_TAKE_PROFIT"


def _capture_release_log(strategy, monkeypatch):
    calls = []
    monkeypatch.setattr(
        strategy, "_log_exit_decision",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


def _on_bar(strategy, ctx, near, far, ts):
    bar = _make_bar(near, far, ts)
    return strategy.on_bar(StrategyContext(
        position=ctx.position,
        market=MarketData(ticker="TMF", last_bar=bar),
        config=ctx.config,
    ))


def test_ilb_far_stop_winner_is_logged_as_release_action(tmp_path, monkeypatch):
    strategy, ctx, _ = _build_strategy(tmp_path, monkeypatch, ilb_enabled=True)
    calls = _capture_release_log(strategy, monkeypatch)

    signal = _on_bar(strategy, ctx, 22000.0, 22086.0,
                     datetime(2026, 11, 24, 9, 0))

    assert signal.action == "PARTIAL_EXIT"
    assert signal.reason == "TMF_RELEASE_FAR"
    assert signal.winner == BRACKET_STOP
    assert calls[-1]["exit_reason"] == BRACKET_STOP


def test_ilb_near_take_profit_winner_is_logged_as_release_action(tmp_path, monkeypatch):
    strategy, ctx, _ = _build_strategy(tmp_path, monkeypatch, ilb_enabled=True)
    calls = _capture_release_log(strategy, monkeypatch)

    signal = _on_bar(strategy, ctx, 22101.0, 22050.0,
                     datetime(2026, 11, 24, 9, 1))

    assert signal.action == "PARTIAL_EXIT"
    assert signal.reason == "TMF_RELEASE_NEAR"
    assert signal.winner == BRACKET_TP
    assert calls[-1]["exit_reason"] == BRACKET_TP


def test_legacy_release_log_remains_release_stop(tmp_path, monkeypatch):
    strategy, ctx, _ = _build_strategy(
        tmp_path, monkeypatch, ilb_enabled=False,
        extra_params={"enable_combined_upl_trail": False},
    )
    calls = _capture_release_log(strategy, monkeypatch)

    signal = _on_bar(strategy, ctx, 21950.0, 22050.0,
                     datetime(2026, 11, 24, 9, 2))

    assert signal.action == "PARTIAL_EXIT"
    assert signal.reason == "TMF_RELEASE_NEAR"
    assert calls[-1]["exit_reason"] == "RELEASE_STOP"


def _monitor_for_release(monkeypatch):
    monitor = make_mock_monitor()
    monkeypatch.setattr(monitor, "_validate_exit_only_position", lambda: (True, None, ""))
    monkeypatch.setattr(monitor, "_exit_only_authorization_ready", lambda strategy: True)
    monkeypatch.setattr(monitor, "_authorize_intent", lambda *a, **k: (True, None, ""))
    monkeypatch.setattr(monitor, "_leg_lock_enabled", lambda: False)
    monkeypatch.setattr(monitor, "_collapse2_bind_exit_order", lambda order: None)
    monkeypatch.setattr(monitor, "_submit_via_gateway", lambda order: True)
    return monitor


def _submit_far_release(monkeypatch, winner):
    monitor = _monitor_for_release(monkeypatch)
    strategy = DummyStrategy()
    strategy._far_side = "SHORT"
    signal = Signal(action="PARTIAL_EXIT", reason="TMF_RELEASE_FAR", winner=winner)
    monitor._submit_mts_order_signal(
        signal, strategy, {"near_close": 22000.0, "far_close": 22050.0},
        datetime(2026, 11, 24, 9, 3),
    )
    events = monitor._append_mts_event.call_args_list
    return signal, [(call.args[0], call.kwargs) for call in events]


def test_monitor_release_events_prefer_bracket_winner_and_preserve_signal_reason(monkeypatch):
    signal, events = _submit_far_release(monkeypatch, BRACKET_STOP)

    assert signal.reason == "TMF_RELEASE_FAR"
    submitted = [data for name, data in events if name == "ORDER_SUBMITTED"]
    leg_submitted = [data for name, data in events if name == "RELEASE_FAR_SUBMITTED"]
    assert submitted[-1]["release_reason"] == BRACKET_STOP
    assert submitted[-1]["reason_source"] == "LIFECYCLE_DECISION"
    assert leg_submitted[-1]["release_reason"] == BRACKET_STOP


def test_monitor_release_events_keep_legacy_fallback_and_reason_source(monkeypatch):
    signal, events = _submit_far_release(monkeypatch, "")

    assert signal.reason == "TMF_RELEASE_FAR"
    submitted = [data for name, data in events if name == "ORDER_SUBMITTED"]
    leg_submitted = [data for name, data in events if name == "RELEASE_FAR_SUBMITTED"]
    assert submitted[-1]["release_reason"] == "TMF_RELEASE_FAR"
    assert submitted[-1]["reason_source"] == "LIFECYCLE_DECISION"
    assert leg_submitted[-1]["release_reason"] == "TMF_RELEASE_FAR"
