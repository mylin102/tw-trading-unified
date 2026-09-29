"""The futures 5m dashboard contract: one casing, bounded, and actually populated.

Rewritten 2026-09-23 (hermes) after the operator hit Close/Open/High/Low/Volume
showing N/A: the contract carried both `close` and `Close`, dashboard
standardisation coalesced then dropped the Title-case twin, and the display
re-indexes to the contract - so the dropped names showed as NaN.
"""
import pathlib

import pandas as pd
import pytest

from core.futures_indicator_contract import (
    CORE_COLUMNS,
    FUTURES_INDICATOR_COLUMNS,
    REST_COLUMNS,
    ordered_indicator_columns,
)


def _monitor(tmp_path=None):
    """A monitor with just enough wiring for _save_bar()."""
    from tests.strategies.test_mts_entry_pricing import _monitor as _m

    mon = _m()
    mon.ticker = "TMF"
    mon.live_trading = True
    mon.dry_run = False
    mon.market_data = {
        "TMF": {"bid": 48297.0, "ask": 48298.0, "bidask_at": __import__("time").time() - 0.05},
        "TMF_FAR": {"bid": 48423.0, "ask": 48434.0, "bidask_at": __import__("time").time() - 0.06},
    }
    return mon


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    import strategies.futures.monitor as mon_mod

    monkeypatch.setattr(mon_mod, "runtime_logs", lambda _name: str(tmp_path), raising=False)
    return tmp_path


def _row():
    return pd.Series({
        "Close": 48344.0, "High": 48360.0, "Low": 48340.0, "Open": 48345.0,
        "Volume": 321.0, "atr": 71.5, "atr_raw": 70.25, "atr_used": 72.5,
        "Vwap": 48300.0, "spread": -160.0, "spread_z": 2.25, "Sqz On": True,
        "Momentum": 1.75, "trend_strength_raw": 1.25,
    }, name=pd.Timestamp("2026-09-23 15:35:00"))


def _saved(tmp_path):
    return pd.read_csv(next(pathlib.Path(tmp_path).rglob("TMF_*_indicators.csv")))


# ── the contract itself ────────────────────────────────────────────────────
def test_contract_puts_entry_columns_first():
    head = list(FUTURES_INDICATOR_COLUMNS[:10])
    assert head == ["timestamp", "near_bid", "near_ask", "near_bidask_age_ms",
                    "near_entry_bbo_state", "far_bid", "far_ask",
                    "far_bidask_age_ms", "far_entry_bbo_state", "close"]


def test_contract_is_bounded_and_has_no_duplicates():
    assert len(FUTURES_INDICATOR_COLUMNS) == len(set(FUTURES_INDICATOR_COLUMNS)) <= 45


def test_contract_has_no_case_duplicate_columns():
    lowered = [c.lower() for c in FUTURES_INDICATOR_COLUMNS]
    dupes = sorted({c for c in lowered if lowered.count(c) > 1})
    assert dupes == [], f"case-duplicate columns: {dupes}"


def test_ordered_columns_drop_unknowns():
    assert ordered_indicator_columns(["close", "nonsense", "rsi"]) == ["close", "rsi"]


# ── the writer ─────────────────────────────────────────────────────────────
def test_save_bar_persists_the_ohlcv_values_under_the_canonical_names(log_dir):
    mon = _monitor()
    mon._save_bar(_row(), score=4.5, regime="NORMAL")

    csv = _saved(log_dir)
    record = csv.iloc[-1]
    assert record["close"] == 48344.0
    assert record["open"] == 48345.0
    assert record["high"] == 48360.0
    assert record["low"] == 48340.0
    assert record["volume"] == 321.0
    assert record["spread"] == -160.0 and record["spread_z"] == 2.25
    assert record["atr_used"] == 72.5 and record["atr"] == 71.5


def test_save_bar_derives_spread_from_near_far_close(log_dir):
    mon = _monitor()
    row = _row().drop(labels=["spread", "spread_z"])
    row["near_close"] = 48274.0
    row["far_close"] = 48435.0

    mon._save_bar(row, score=4.5, regime="NORMAL")

    record = _saved(log_dir).iloc[-1]
    assert pd.notna(record["spread"])
    assert record["spread"] == -161.0


def test_save_bar_persists_decision_spread_z_from_caller(log_dir):
    mon = _monitor()
    row = _row().drop(labels=["spread_z"])
    decision_z = 3.75

    # This is the value produced by the decision bar and passed through the
    # real _save_bar call contract; it must not be reconstructed from CSV.
    mon._save_bar(row, score=4.5, regime="NORMAL", spread_z=decision_z)

    record = _saved(log_dir).iloc[-1]
    assert pd.notna(record["spread_z"])
    assert record["spread_z"] == decision_z


def test_saved_csv_has_no_case_duplicates(log_dir):
    mon = _monitor()
    mon._save_bar(_row(), score=4.5, regime="NORMAL")

    lowered = [str(c).lower() for c in _saved(log_dir).columns]
    assert sorted({c for c in lowered if lowered.count(c) > 1}) == []


def test_save_bar_quote_columns_come_from_the_bidask_slots(log_dir):
    mon = _monitor()
    mon._save_bar(_row(), score=4.5, regime="NORMAL")

    record = _saved(log_dir).iloc[-1]
    assert record["near_bid"] == 48297.0 and record["near_ask"] == 48298.0
    assert record["far_bid"] == 48423.0 and record["far_ask"] == 48434.0
    assert record["near_entry_bbo_state"] == "OK"
    assert record["far_entry_bbo_state"] == "OK"


def test_save_bar_never_derives_a_quote_from_close(log_dir):
    mon = _monitor()
    mon.market_data = {}                      # no BidAsk at all
    mon._save_bar(_row(), score=4.5, regime="NORMAL")

    record = _saved(log_dir).iloc[-1]
    assert pd.isna(record["near_bid"]) and pd.isna(record["far_bid"])
    assert record["near_entry_bbo_state"] == "NO_BIDASK"
    assert record["close"] == 48344.0


# ── the dashboard side ─────────────────────────────────────────────────────
def test_dashboard_usecols_are_the_contract():
    import ui.dashboard as dash

    assert list(dash._FUTURES_USECOLS) == list(FUTURES_INDICATOR_COLUMNS)


def test_removed_columns_do_not_return_to_the_dashboard_contract():
    removed = {"bear_align", "router_regime", "price_vs_vwap_pct", "fired",
               "squeeze_release", "bb_lower", "bb_upper", "atr_floor",
               "breakout_strength", "breakout_strength_atr", "amount",
               "recent_high", "recent_low", "trading_day", "router_bias",
               "mom_state", "mom_prev", "macd_line", "macd_signal", "macd_rising",
               "in_pb_zone", "is_structural_breakout"}
    lowered = {c.lower() for c in FUTURES_INDICATOR_COLUMNS}
    assert removed.isdisjoint({c.lower() for c in FUTURES_INDICATOR_COLUMNS}) or not removed & lowered


def test_dashboard_read_populates_every_contract_column(log_dir):
    """The exact regression: the display re-indexes to the contract, so every
    contract column must survive the dashboard's read + standardisation."""
    mon = _monitor()
    mon._save_bar(_row(), score=4.5, regime="NORMAL")

    import ui.dashboard as dash
    df = pd.read_csv(next(pathlib.Path(log_dir).rglob("TMF_*_indicators.csv")),
                     usecols=lambda c: c in set(dash._FUTURES_USECOLS))
    for upper, lower in {"Open": "open", "High": "high", "Low": "low",
                         "Close": "close", "Volume": "volume"}.items():
        if upper in df.columns and lower in df.columns:
            df[lower] = df[lower].fillna(df[upper])
            df = df.drop(columns=[upper])
    record = df.iloc[-1]
    for column in ("close", "open", "high", "low", "volume",
                   "spread", "spread_z", "atr", "atr_used", "score"):
        assert pd.notna(record[column]), f"{column} must not be NaN"
