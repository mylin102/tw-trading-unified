"""Evaluator-lag SLO tests (2026-08-06, codex audit follow-up, v2.1).

Split-clock design:
  - tick_loop clock compares the PREVIOUS completed tick (captured before
    stamping this tick) so a whole-loop stall is observed on the first tick
    after recovery — exercised through the real stamp -> check sequence.
  - on_bar clock: heartbeat after strategy.on_bar() returns.
Suppressed while FLAT or market closed. Fresh pending MTS orders (transition
window) suppress; orders older than pending_order_stall_secs alert as
TRANSITION_STALL. Baselines seeded at startup so a restored OPEN position
with no first evaluation alerts after grace (first_eval=True).
"""
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from strategies.futures.monitor import FuturesMonitor


def make_mon():
    with patch.object(FuturesMonitor, "__init__", lambda self: None):
        mon = FuturesMonitor()
    mon.cfg = {
        "mts": {
            "params": {
                "tick_loop_slo_secs": 90.0,
                "on_bar_slo_secs": 90.0,
                "pending_order_stall_secs": 120.0,
            }
        }
    }
    _now = time.monotonic()
    mon._startup_mono = _now - 500.0  # started long ago
    mon._last_mts_tick_mono = _now          # ticks flowing
    mon._prev_mts_tick_mono = _now          # previous tick fresh
    mon._last_mts_tick_wall = "tick-now"
    mon._last_strategy_evaluation_mono = mon._startup_mono  # on_bar never ran
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
    mon._mts_note_strategy_evaluated()  # on_bar heartbeat fresh
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_bar_gated_cadence_respected():
    mon = make_mon()
    mon.cfg["mts"]["params"]["on_bar_slo_secs"] = 660.0  # 2x 5m bar + grace
    mon._last_strategy_evaluation_mono = time.monotonic() - 300.0  # one bar ago
    mon._strategy_evaluated_once = True
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


# ── split clocks ──

def test_tick_loop_stall_detected_through_real_sequence():
    """Real _mts_tick order: stamp() captures prev, then check() runs.

    A 200s whole-loop stall must be observed on the first tick after
    recovery (this is the runtime-impossible state the v2 unit test missed).
    """
    mon = make_mon()
    mon._strategy_evaluated_once = True
    mon._last_strategy_evaluation_mono = time.monotonic()  # on_bar fresh
    mon._prev_mts_tick_mono = time.monotonic() - 200.0  # stall happened
    mon._last_mts_tick_mono = time.monotonic() - 200.0
    mon._mts_stamp_tick_loop()  # real order: stamp first
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)  # then check
    _clocks = [c.kwargs["clock"] for c in mon._append_mts_event.call_args_list]
    assert "tick_loop" in _clocks, f"clocks={_clocks}"
    _ev = [c for c in mon._append_mts_event.call_args_list
           if c.kwargs["clock"] == "tick_loop"][0]
    assert _ev.kwargs["lag_secs"] >= 190.0


def test_on_bar_stale_fires_alert():
    mon = make_mon()
    mon._strategy_evaluated_once = True
    mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
    mon._mts_check_evaluator_lag(_Strat("t1"), strat_has_pos=True)
    mon._append_mts_event.assert_called_once()
    _ev = mon._append_mts_event.call_args
    assert _ev.args[0] == "MTS_EVALUATOR_LAG"
    assert _ev.kwargs["clock"] == "on_bar"
    assert _ev.kwargs["trade_id"] == "t1"
    assert _ev.kwargs["lag_secs"] >= 190.0
    assert _ev.kwargs["slo_secs"] == 90.0
    assert _ev.kwargs["first_eval"] is False


# ── startup / restoration gap ──

def test_restored_open_no_first_eval_alerts():
    mon = make_mon()
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


# ── suppression / pending orders ──

def test_transition_pending_fresh_suppressed():
    mon = make_mon()
    mon.order_mgr = SimpleNamespace(
        active_orders=[
            SimpleNamespace(strategy="MTS_ENTRY", created_at=datetime.now())
        ]
    )
    mon._last_strategy_evaluation_mono = time.monotonic() - 500.0
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_transition_pending_hung_alerts_stall():
    mon = make_mon()
    mon.order_mgr = SimpleNamespace(
        active_orders=[
            SimpleNamespace(
                strategy="MTS_ENTRY",
                created_at=datetime.now().replace(second=0) - __import__(
                    "datetime").timedelta(minutes=10),
            )
        ]
    )
    mon._last_strategy_evaluation_mono = time.monotonic() - 500.0
    mon._mts_check_evaluator_lag(_Strat("t7"), strat_has_pos=True)
    mon._append_mts_event.assert_called_once()
    _ev = mon._append_mts_event.call_args
    assert _ev.kwargs["clock"] == "pending_stall"
    assert _ev.kwargs["slo_secs"] == 120.0
    assert _ev.kwargs["lag_secs"] >= 500.0


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
    mon._strategy_evaluated_once = True
    mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
    mon._last_slo_alert_mono["on_bar"] = time.monotonic()  # alerted just now
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_invalid_config_falls_back_to_default():
    for bad in ("abc", 0, -5, float("nan")):
        mon = make_mon()
        mon.cfg["mts"]["params"]["on_bar_slo_secs"] = bad
        mon._strategy_evaluated_once = True
        mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
        mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
        _ev = mon._append_mts_event.call_args
        assert _ev.kwargs["slo_secs"] == 90.0, f"bad config {bad!r}"


def test_event_write_failure_tolerated():
    mon = make_mon()
    mon._strategy_evaluated_once = True
    mon._last_strategy_evaluation_mono = time.monotonic() - 200.0
    mon._append_mts_event.side_effect = OSError("disk full")
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)  # must not raise


def test_note_strategy_evaluated_sets_timestamps_and_flag():
    mon = make_mon()
    mon._mts_note_strategy_evaluated()
    assert mon._last_strategy_evaluation_mono is not None
    assert isinstance(mon._last_strategy_evaluation_wall, str)
    assert mon._strategy_evaluated_once is True
