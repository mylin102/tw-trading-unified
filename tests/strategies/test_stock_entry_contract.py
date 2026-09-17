from pathlib import Path

import pandas as pd
import pytest

from strategies.stocks.multi_timeframe import analyze_market_condition
from strategies.stocks.monitor import StockMonitor


def _df(columns=("open", "high", "low", "close", "volume"), rows=35):
    return pd.DataFrame({column: [100 + i for i in range(rows)] for column in columns},
                        index=pd.date_range("2026-09-17", periods=rows, freq="5min"))


def test_t1_one_failure_warns_and_other_tickers_remain_isolated(capsys):
    monitor = StockMonitor.__new__(StockMonitor)
    monitor._analysis_failures = []
    monitor._analysis_total = 2
    monitor._record_analysis_failure("2330", KeyError("Close"))
    monitor._finish_analysis_round()
    output = capsys.readouterr().out
    assert "WARN" in output and "2330" in output and "KeyError" in output
    assert "1/2" in output
    assert "ALL" not in output


def test_t2_all_failures_raise_round_alert(capsys):
    monitor = StockMonitor.__new__(StockMonitor)
    monitor._analysis_failures = []
    monitor._analysis_total = 2
    for ticker in ("2330", "2317"):
        monitor._record_analysis_failure(ticker, KeyError("Close"))
    monitor._finish_analysis_round()
    output = capsys.readouterr().out
    assert "ERROR" in output and "2/2" in output


def test_t3_lowercase_ohlcv_is_normalized_at_analysis_entry():
    result = analyze_market_condition(_df())
    assert isinstance(result, dict)


def test_t4_missing_required_column_is_explicit_error():
    with pytest.raises(ValueError, match="STOCK_OHLCV_SCHEMA_MISSING.*Close"):
        analyze_market_condition(_df(("open", "high", "low", "volume")))


def test_mutation_silent_except_is_rejected():
    source = (Path(__file__).resolve().parents[2] / "strategies/stocks/monitor.py").read_text()
    assert "self._record_analysis_failure(ticker, e)" in source
    assert "self._finish_analysis_round()" in source
