"""Evaluator-lag SLO tests (2026-08-06, codex audit follow-up, v2).

Split-clock design:
  - tick_loop clock: heartbeat stamped on every _mts_tick evaluation pass
  - on_bar clock: heartbeat stamped after strategy.on_bar() returns
Both alert (rate-limited 60s) when a position is open and the clock is stale
past its SLO (mts.params.{tick_loop,on_bar}_slo_secs, default 90).
Suppressed while FLAT, market closed, or MTS orders pending (transition
window). Baselines are seeded at startup so a restored OPEN position with no
first evaluation alerts after grace (first_eval=True).
"""
import time
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from strategies.futures.monitor import FuturesMonitor


def make_mon():
    with patch.object(FuturesMonitor, "__init__", lambda self: None):
        mon = FuturesMonitor()
    mon.cfg = {
        "mts": {"params": {"tick_loop_slo_secs": 90.0, "on_bar_slo_secs": 90.0}}
    }
    mon._startup_mono = time.monotonic() - 500.0  # started long ago
    mon._last_mts_tick_mono = mon._startup_mono
    mon._last_mts_tick_wall = "startup"
    mon._last_strategy_evaluation_mono = mon._startup_mono
    mon._last_strategy_evaluation_wall = "startup"
    mon._strategy_evaluated_once = False
    mon._last_slo_alert_mono = {}
    mon._append_mts_event = MagicMock()
    mon.order_mgr = MagicMock()  # no pending orders
    return mon


class _Strat:
    def __init__(self, trade_id="t1"):
        self._trade_id = trade_id


# ── cadence semantics ──

def test_healthy_tick_cadence_no_alert():
    mon = make_mon()
    mon._last_mts_tick_mono = time.monotonic()
    mon._last_strategy_evaluation_mono = time.monotonic()
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_bar_gated_cadence_respected():
    mon = make_mon()
    mon.cfg["mts"]["params"]["on_bar_slo_secs"] = 660.0  # 2x 5m bar + grace
    mon._last_mts_tick_mono = time.monotonic()
    mon._last_strategy_evaluation_mono = time.monotonic() - 300.0  # one bar ago
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


# ── split clocks ──

def test_on_bar_stale_fires_alert():
    mon = make_mon()
    mon._last_mts_tick_mono = time.monotonic()  # tick loop alive
    mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
    mon._strategy_evaluated_once = True
    mon._mts_check_evaluator_lag(_Strat("t1"), strat_has_pos=True)
    mon._append_mts_event.assert_called_once()
    _ev = mon._append_mts_event.call_args
    assert _ev.args[0] == "MTS_EVALUATOR_LAG"
    assert _ev.kwargs["clock"] == "on_bar"
    assert _ev.kwargs["trade_id"] == "t1"
    assert _ev.kwargs["lag_secs"] >= 190.0
    assert _ev.kwargs["slo_secs"] == 90.0
    assert _ev.kwargs["first_eval"] is False


def test_tick_loop_stale_fires_alert():
    mon = make_mon()
    mon._last_mts_tick_mono = time.monotonic() - 200.0
    mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    _clocks = [c.kwargs["clock"] for c in mon._append_mts_event.call_args_list]
    assert "tick_loop" in _clocks
    assert "on_bar" in _clocks


# ── startup / restoration gap ──

def test_restored_open_no_first_eval_alerts():
    mon = make_mon()
    mon._last_mts_tick_mono = time.monotonic()  # ticks flow, on_bar never ran
    mon._last_mts_tick_wall = "tick-now"
    mon._strategy_evaluated_once = False  # restored OPEN, never evaluated
    mon._mts_check_evaluator_lag(_Strat("t9"), strat_has_pos=True)
    mon._append_mts_event.assert_called_once()
    _ev = mon._append_mts_event.call_args
    assert _ev.kwargs["clock"] == "on_bar"
    assert _ev.kwargs["first_eval"] is True
    assert _ev.kwargs["trade_id"] == "t9"


def test_first_eval_flag_clears_after_first_evaluation():
    mon = make_mon()
    mon._mts_note_strategy_evaluated()
    assert mon._strategy_evaluated_once is True
    mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    _ev = mon._append_mts_event.call_args
    assert _ev.kwargs["first_eval"] is False


# ── suppression ──

def test_transition_pending_suppressed():
    mon = make_mon()
    mon.order_mgr = SimpleNamespace(
        active_orders=[SimpleNamespace(strategy="MTS_ENTRY")]
    )
    mon._last_strategy_evaluation_mono = time.monotonic() - 500.0
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_market_closed_suppressed():
    mon = make_mon()
    mon._last_strategy_evaluation_mono = time.monotonic() - 500.0
    with patch("strategies.futures.monitor.is_taifex_futures_market_open",
               return_value=False):
        mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_flat_position_no_alert_even_if_stale():
    mon = make_mon()
    mon._last_strategy_evaluation_mono = time.monotonic() - 500.0
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=False)
    mon._append_mts_event.assert_not_called()


# ── rate limit / config / failure handling ──

def test_rate_limited_alert():
    mon = make_mon()
    mon._last_mts_tick_mono = time.monotonic()  # tick loop fresh
    mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
    mon._last_slo_alert_mono["on_bar"] = time.monotonic()  # alerted just now
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_invalid_config_falls_back_to_default():
    for bad in ("abc", 0, -5, float("nan")):
        mon = make_mon()
        mon.cfg["mts"]["params"]["on_bar_slo_secs"] = bad
        mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
        mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
        _ev = mon._append_mts_event.call_args
        assert _ev.kwargs["slo_secs"] == 90.0, f"bad config {bad!r}"


def test_event_write_failure_tolerated():
    mon = make_mon()
    mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
    mon._append_mts_event.side_effect = OSError("disk full")
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)  # must not raise


def test_note_strategy_evaluated_sets_timestamps_and_flag():
    mon = make_mon()
    mon._mts_note_strategy_evaluated()
    assert mon._last_strategy_evaluation_mono is not None
    assert isinstance(mon._last_strategy_evaluation_wall, str)
    assert mon._strategy_evaluated_once is True
