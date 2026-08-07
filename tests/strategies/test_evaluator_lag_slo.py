"""Evaluator-lag SLO tests (2026-08-06, codex audit follow-up).

While a position is open, FuturesMonitor's strategy evaluator (on_bar) must
heartbeat. If it goes silent longer than mts.params.evaluator_lag_slo_secs
(default 90), a rate-limited warning + MTS_EVALUATOR_LAG event is emitted.
This catches the o-091403-145 class: position open but zero evaluation for
hours.
"""
import time
from unittest.mock import patch, MagicMock

from strategies.futures.monitor import FuturesMonitor


def make_mon():
    with patch.object(FuturesMonitor, "__init__", lambda self: None):
        mon = FuturesMonitor()
    mon.cfg = {"mts": {"params": {"evaluator_lag_slo_secs": 90.0}}}
    mon._last_strategy_evaluation_mono = None
    mon._last_strategy_evaluation_wall = None
    mon._last_evaluator_lag_alert_mono = 0.0
    mon._append_mts_event = MagicMock()
    return mon


class _Strat:
    def __init__(self, trade_id="t1"):
        self._trade_id = trade_id


def test_fresh_evaluation_no_alert():
    mon = make_mon()
    mon._last_strategy_evaluation_mono = time.monotonic()
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_stale_evaluation_fires_alert():
    mon = make_mon()
    mon.cfg["mts"]["params"]["evaluator_lag_slo_secs"] = 0.01  # tiny SLO
    mon._last_strategy_evaluation_mono = time.monotonic() - 5.0  # 5s stale
    mon._mts_check_evaluator_lag(_Strat("t1"), strat_has_pos=True)
    mon._append_mts_event.assert_called_once()
    _ev = mon._append_mts_event.call_args
    assert _ev.args[0] == "MTS_EVALUATOR_LAG"
    assert _ev.kwargs["trade_id"] == "t1"
    assert _ev.kwargs["lag_secs"] >= 4.0


def test_flat_position_no_alert_even_if_stale():
    mon = make_mon()
    mon._last_strategy_evaluation_mono = time.monotonic() - 500.0
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=False)
    mon._append_mts_event.assert_not_called()


def test_never_evaluated_no_alert():
    mon = make_mon()
    mon._last_strategy_evaluation_mono = None
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_rate_limited_alert():
    mon = make_mon()
    mon.cfg["mts"]["params"]["evaluator_lag_slo_secs"] = 0.01
    mon._last_strategy_evaluation_mono = time.monotonic() - 5.0
    mon._last_evaluator_lag_alert_mono = time.monotonic()  # alerted recently
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_not_called()


def test_alert_after_rate_limit_window():
    mon = make_mon()
    mon.cfg["mts"]["params"]["evaluator_lag_slo_secs"] = 0.01
    mon._last_strategy_evaluation_mono = time.monotonic() - 5.0
    mon._last_evaluator_lag_alert_mono = time.monotonic() - 120.0
    mon._mts_check_evaluator_lag(_Strat(), strat_has_pos=True)
    mon._append_mts_event.assert_called_once()


def test_note_strategy_evaluated_sets_timestamps():
    mon = make_mon()
    mon._mts_note_strategy_evaluated()
    assert mon._last_strategy_evaluation_mono is not None
    assert mon._last_strategy_evaluation_wall is not None
    assert isinstance(mon._last_strategy_evaluation_wall, str)
