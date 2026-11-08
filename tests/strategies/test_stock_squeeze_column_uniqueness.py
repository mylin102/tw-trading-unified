"""2026-11-08 Hermes Agent: calculate_stock_squeeze must not emit case-variant duplicates.

Incident: stock-runner made zero entries for 10+ trading days. The round log
(eventually surfaced after removing a bare `except: pass`) showed ALL 25 tickers
failing with:
    ValueError: STOCK_OHLCV_SCHEMA_DUPLICATE: trading_day
multi_timeframe._normalize_ohlcv_columns rejects a frame holding two columns
whose names differ only by case.

Cause: indicators.py adds `df["trading_day"]` (lowercase, line ~49), then
`df.columns = [c.capitalize() for c in df.columns]` (line ~70) rewrites it to
`Trading_day`, and the later `res["trading_day"] = ...` re-adds the lowercase
one -- so the frame leaves the function with BOTH. No consumer reads the
capitalised spelling.
"""
import inspect
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.options.options_engine.engine import indicators


def _frame(n=40):
    idx = pd.date_range("2026-11-04 09:00", periods=n, freq="5min")
    c = 100 + np.cumsum(np.random.default_rng(0).normal(0, 0.3, n))
    df = pd.DataFrame({"Open": c, "High": c + .3, "Low": c - .3, "Close": c,
                       "Volume": 1000, "Amount": 1e6}, index=idx)
    df.index.name = "ts"
    return df


def test_no_case_variant_duplicate_columns():
    out = indicators.calculate_stock_squeeze(_frame())
    lowered = [str(c).lower() for c in out.columns]
    dupes = {c for c in lowered if lowered.count(c) > 1}
    assert not dupes, f"case-variant duplicate columns: {sorted(dupes)}"


def test_trading_day_survives_lowercase_for_groupby_consumers():
    out = indicators.calculate_stock_squeeze(_frame())
    assert "trading_day" in out.columns
    assert "Trading_day" not in out.columns


def test_normalizer_accepts_the_produced_frame():
    from strategies.stocks.multi_timeframe import analyze_market_condition
    out = indicators.calculate_stock_squeeze(_frame())
    analyze_market_condition(out)      # must not raise STOCK_OHLCV_SCHEMA_DUPLICATE


def test_source_still_capitalises_ohlcv_columns():
    src = Path(inspect.getsourcefile(indicators)).read_text()
    assert "capitalize()" in src
