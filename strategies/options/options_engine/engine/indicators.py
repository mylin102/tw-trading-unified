import os

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")

import numpy as np
import pandas as pd
import pandas_ta as ta


def calculate_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    if df.empty or len(df) < length:
        return pd.Series(index=df.index, dtype=float)

    high = df["High"]
    low = df["Low"]
    close_prev = df["Close"].shift(1)

    tr1 = high - low
    tr2 = abs(high - close_prev)
    tr3 = abs(low - close_prev)
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=length).mean()


def calculate_futures_squeeze(
    df: pd.DataFrame,
    bb_length=14,
    bb_std=2.0,
    kc_length=14,
    kc_scalar=1.5,
    ema_fast=20,
    ema_slow=60,
    lookback=60,
    pb_buffer=1.002,
    ema_macro=200,
) -> pd.DataFrame:
    # Minimum length required for basic squeeze calculation
    min_req = max(bb_length, kc_length, 30)
    if df.empty or len(df) < min_req:
        return df

    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(-1)
    df.columns = [c.capitalize() for c in df.columns]

    sqz = df.ta.squeeze(
        bb_length=bb_length,
        bb_std=bb_std,
        kc_length=kc_length,
        kc_scalar=kc_scalar,
        lazy=True,
    )
    res = df.copy()

    sqz_on_cols = [c for c in sqz.columns if "SQZ_ON" in c]
    res["sqz_on"] = sqz[sqz_on_cols[0]].astype(bool) if sqz_on_cols else False

    mom_cols = [c for c in sqz.columns if "SQZ_" in c and not any(x in c for x in ["ON", "OFF", "NO"])]
    res["momentum"] = sqz[mom_cols[0]].fillna(0) if mom_cols else 0
    
    # Ensure mom_state is calculated even if resample drops it
    res["mom_prev"] = res["momentum"].shift(1).fillna(0)

    # 改進 VWAP：使用交易日 (Trading Day) 而非日曆日 (Calendar Day)
    # 台指期規則：15:00 以後屬下一個交易日
    res["trading_day"] = (res.index + pd.Timedelta(hours=9)).date
    
    typical_price_x_volume = res["Close"] * res["Volume"]
    volume_cumsum = res.groupby("trading_day")["Volume"].cumsum()
    res["vwap"] = typical_price_x_volume.groupby(res["trading_day"]).cumsum() / volume_cumsum
    res["vwap"] = res["vwap"].where(volume_cumsum != 0, res["Close"])
    res["price_vs_vwap"] = np.where(res["vwap"] != 0, (res["Close"] - res["vwap"]) / res["vwap"], 0.0)
    res["fired"] = (~res["sqz_on"]) & (res["sqz_on"].shift(1) == True)

    # Calculate mom_state vectorized for better performance
    m = res["momentum"].values
    p = res["mom_prev"].values
    res["mom_state"] = np.select(
        [
            (m > 0) & (m >= p),
            (m > 0) & (m < p),
            (m <= 0) & (m <= p),
            (m <= 0) & (m > p)
        ],
        [3, 2, 0, 1],
        default=1
    )
    
    # Safe EMA calculation
    def safe_ema(length):
        if len(df) >= length:
            return df.ta.ema(length=length)
        return df.ta.ema(length=len(df)) if len(df) > 1 else res["Close"]

    res["ema_fast"] = safe_ema(ema_fast)
    res["ema_slow"] = safe_ema(ema_slow)
    res["ema_filter"] = safe_ema(60)
    res["ema_macro"] = safe_ema(ema_macro)
    
    res["bullish_align"] = res["ema_fast"] > res["ema_slow"]
    res["bearish_align"] = res["ema_fast"] < res["ema_slow"]

    cur_lookback = min(lookback, len(df) - 1) if len(df) > 1 else 1
    res["recent_high"] = res["Close"].rolling(window=cur_lookback).max()
    res["recent_low"] = res["Close"].rolling(window=cur_lookback).min()
    res["is_new_high"] = res["Close"] >= res["recent_high"].shift(1)
    res["is_new_low"] = res["Close"] <= res["recent_low"].shift(1)
    res["in_bull_pb_zone"] = (
        (res["Close"] <= res["ema_fast"] * pb_buffer)
        & (res["Close"] >= res["ema_slow"])
        & res["bullish_align"]
    )
    res["in_bear_pb_zone"] = (
        (res["Close"] >= res["ema_fast"] * (2 - pb_buffer))
        & (res["Close"] <= res["ema_slow"])
        & res["bearish_align"]
    )

    res["day_open"] = res.groupby("trading_day")["Open"].transform("first")
    res["day_min"] = res.groupby("trading_day")["Low"].cummin()
    res["day_max"] = res.groupby("trading_day")["High"].cummax()
    res["opening_bullish"] = (res["Close"] > res["day_open"]) & (res["day_min"] >= res["day_open"] * 0.999)
    res["opening_bearish"] = (res["Close"] < res["day_open"]) & (res["day_max"] <= res["day_open"] * 1.001)

    return res


def calculate_mtf_alignment(data_dict: dict[str, pd.DataFrame], weights=None) -> dict:
    if not data_dict:
        return {"score": 0, "is_aligned": False}
    if weights is None:
        weights = {"1h": 0.2, "15m": 0.4, "5m": 0.4}

    latest_states = {}
    for timeframe, df in data_dict.items():
        if df.empty:
            continue
        last = df.iloc[-1]
        direction = 1 if last["momentum"] > 0 else -1
        strength = 1.5 if last["mom_state"] in [0, 3] else 1.0
        latest_states[timeframe] = direction * strength

    total_score = 0.0
    available_weight = 0.0
    for timeframe, value in latest_states.items():
        weight = weights.get(timeframe, 0.1)
        total_score += value * weight
        available_weight += weight

    if available_weight <= 0:
        return {"score": 0, "is_aligned": False}
    return {"score": (total_score / (1.5 * available_weight)) * 100}
