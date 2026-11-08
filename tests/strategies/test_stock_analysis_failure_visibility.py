"""2026-11-08 Hermes Agent: the main scan loop must not swallow analysis errors.

Incident: stock-runner produced ZERO entries for 10+ trading days (2026-10-30 ..
11-08, every STOCK_*_PAPER_orders.json was an empty dict). The log showed each
ticker reaching "Resampled <ticker> to 5-minute bars (165 rows)" and then
nothing -- run_iteration's per-ticker handler ended in a bare `pass`, so any
exception raised by the indicator/multi-timeframe stage vanished with no
evidence at all.

The helper `_record_analysis_failure()` already existed and was wired into the
other scan path (line ~483); only the main loop skipped it.
"""
import inspect
import re
from pathlib import Path

import strategies.stocks.monitor as mod


def _source() -> str:
    return Path(inspect.getsourcefile(mod)).read_text()


def test_main_loop_records_analysis_failures_instead_of_passing():
    src = _source()
    i = src.index("def run_iteration(")
    j = src.index("def run(", i)          # run_iteration ends before the legacy run()
    body = src[i:j]

    assert "classify_broker_error" in body, "session-death guard must stay"
    # the per-ticker handler must record, not swallow
    assert "self._record_analysis_failure(ticker, e)" in body, (
        "run_iteration swallowed a per-ticker exception without recording it")
    # and it must still re-raise session death
    assert "raise" in body


def test_recording_a_failure_is_visible_to_the_round_summary():
    mon = object.__new__(mod.StockMonitor)
    mon._analysis_failures = []
    mon._analysis_total = 3
    mon._record_analysis_failure("2330", KeyError("Close"))
    assert mon._analysis_failures == ["2330"]
