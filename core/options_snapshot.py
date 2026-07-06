from __future__ import annotations

import datetime

import pandas as pd

from core.date_utils import get_session, get_trading_day


OPTION_SNAPSHOT_COLUMNS = [
    "timestamp",
    "trading_day",
    "session",
    "score",
    "side",
    "price_mtx",
    "strike",
    "dte",
    "mid_trend",
    "iv",
    "delta",
    "gamma",
    "vega",
    "vwap",
    "squeeze_on",
    "sqz_on",
    "fired",
    "bullish_align",
    "bearish_align",
    "bull_align",
    "bear_align",
    "bullish_sign",
    "bearlish_sign",
    "bearish_sign",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "open",
    "high",
    "low",
    "close",
    "volume",
]


def _coalesce(row: dict, keys: list[str], default):
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if pd.isna(value):
            continue
        return value
    return default


def _coerce_bool(value) -> bool:
    if pd.isna(value) or value is None:
        return False
    return bool(value)


def build_options_snapshot_row(
    signal: dict | None,
    *,
    now: datetime.datetime,
    price_mtx: float,
    score: float,
    side_label: str,
    strike: float,
    dte_days: float,
    mid_trend: str,
    iv: float,
    delta_val: float,
    gamma_val: float,
    vega_val: float,
) -> dict:
    row = signal.copy() if signal else {}
    close = float(_coalesce(row, ["Close", "close", "price_mtx"], price_mtx))
    open_ = float(_coalesce(row, ["Open", "open"], close))
    high = float(_coalesce(row, ["High", "high"], max(open_, close)))
    low = float(_coalesce(row, ["Low", "low"], min(open_, close)))
    volume = float(_coalesce(row, ["Volume", "volume"], 0.0))
    sqz_on = _coerce_bool(_coalesce(row, ["sqz_on", "squeeze_on"], False))
    fired = _coerce_bool(row.get("fired", False))
    bullish_align = _coerce_bool(_coalesce(row, ["bullish_align", "bull_align", "bullish_sign"], False))
    bearish_align = _coerce_bool(
        _coalesce(row, ["bearish_align", "bear_align", "bearlish_sign", "bearish_sign"], False)
    )
    trading_day = get_trading_day(now).strftime("%Y-%m-%d")

    row.update(
        {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trading_day": trading_day,
            "session": get_session(now),
            "score": float(score),
            "side": side_label,
            "price_mtx": float(price_mtx),
            "strike": float(strike),
            "dte": round(float(dte_days), 2),
            "mid_trend": mid_trend or "",
            "iv": round(float(iv), 4),
            "delta": round(float(delta_val), 4),
            "gamma": round(float(gamma_val), 6),
            "vega": round(float(vega_val), 4),
            "vwap": float(_coalesce(row, ["vwap"], price_mtx)),
            "squeeze_on": sqz_on,
            "sqz_on": sqz_on,
            "fired": fired,
            "bullish_align": bullish_align,
            "bearish_align": bearish_align,
            "bull_align": bullish_align,
            "bear_align": bearish_align,
            "bullish_sign": bullish_align,
            "bearlish_sign": bearish_align,
            "bearish_sign": bearish_align,
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )

    for column in OPTION_SNAPSHOT_COLUMNS:
        if column not in row:
            row[column] = None
    return row
