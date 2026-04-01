import pandas as pd
import numpy as np

try:
    import pandas_ta as ta  # noqa: F401
except Exception:
    ta = None


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close_prev = df["Close"].shift(1)
    return pd.concat(
        [
            high - low,
            (high - close_prev).abs(),
            (low - close_prev).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _fallback_squeeze(
    df: pd.DataFrame,
    bb_length: int,
    bb_std: float,
    kc_length: int,
    kc_scalar: float,
) -> tuple[pd.Series, pd.Series]:
    close = df["Close"]
    basis = close.rolling(window=bb_length, min_periods=bb_length).mean()
    deviation = close.rolling(window=bb_length, min_periods=bb_length).std(ddof=0)
    bb_upper = basis + deviation * bb_std
    bb_lower = basis - deviation * bb_std

    ema_basis = _ema(close, kc_length)
    atr = _true_range(df).rolling(window=kc_length, min_periods=kc_length).mean()
    kc_upper = ema_basis + atr * kc_scalar
    kc_lower = ema_basis - atr * kc_scalar

    sqz_on = (bb_lower >= kc_lower) & (bb_upper <= kc_upper)
    momentum = close - basis
    return sqz_on.fillna(False), momentum.fillna(0.0)


def _pandas_ta_squeeze(
    df: pd.DataFrame,
    bb_length: int,
    bb_std: float,
    kc_length: int,
    kc_scalar: float,
) -> tuple[pd.Series, pd.Series]:
    if ta is None:
        raise RuntimeError("pandas_ta unavailable")

    sqz = df.ta.squeeze(
        bb_length=bb_length,
        bb_std=bb_std,
        kc_length=kc_length,
        kc_scalar=kc_scalar,
        lazy=True,
    )

    sqz_on_cols = [c for c in sqz.columns if "SQZ_ON" in c]
    mom_cols = [
        c for c in sqz.columns if "SQZ_" in c and not any(x in c for x in ["ON", "OFF", "NO"])
    ]
    sqz_on = sqz[sqz_on_cols[0]].astype(bool) if sqz_on_cols else pd.Series(False, index=df.index)
    momentum = sqz[mom_cols[0]].fillna(0.0) if mom_cols else pd.Series(0.0, index=df.index)
    return sqz_on, momentum


def calculate_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """
    計算 ATR (Average True Range)

    Args:
        df: 包含 High, Low, Close 的 DataFrame
        length: ATR 計算週期，預設 14

    Returns:
        ATR Series
    """
    if df.empty or len(df) < length:
        return pd.Series(index=df.index, dtype=float)

    return _true_range(df).rolling(window=length).mean()


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
    """
    包含 Squeeze、雙向回測、環境過濾及開盤法判定的指標計算。
    """
    df = df.copy()
    if df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(-1)
    df.columns = [c.capitalize() for c in df.columns]

    min_required = max(bb_length, ema_slow, lookback, ema_macro)
    if len(df) < min_required:
        df["sqz_on"] = False
        df["momentum"] = 0.0
        df["vwap"] = df["Close"]
        df["price_vs_vwap"] = 0.0
        df["fired"] = False
        df["mom_prev"] = 0.0
        df["mom_state"] = 1
        df["ema_fast"] = df["Close"]
        df["ema_slow"] = df["Close"]
        df["ema_filter"] = df["Close"]
        df["ema_macro"] = df["Close"]
        df["bullish_align"] = False
        df["bearish_align"] = False
        df["recent_high"] = df["Close"]
        df["recent_low"] = df["Close"]
        df["is_new_high"] = False
        df["is_new_low"] = False
        df["in_bull_pb_zone"] = False
        df["in_bear_pb_zone"] = False
        df["day_open"] = df["Open"]
        df["day_min"] = df["Low"]
        df["day_max"] = df["High"]
        df["opening_bullish"] = False
        df["opening_bearish"] = False
        return df

    try:
        sqz_on, momentum = _pandas_ta_squeeze(df, bb_length, bb_std, kc_length, kc_scalar)
    except Exception:
        sqz_on, momentum = _fallback_squeeze(df, bb_length, bb_std, kc_length, kc_scalar)

    res = df.copy()
    res["sqz_on"] = sqz_on
    res["momentum"] = momentum
    res["date"] = res.index.date
    typical_price_x_volume = res["Close"] * res["Volume"]
    volume_cumsum = res["Volume"].groupby(res["date"]).cumsum()
    res["vwap"] = typical_price_x_volume.groupby(res["date"]).cumsum() / volume_cumsum
    res["vwap"] = res["vwap"].where(volume_cumsum != 0, res["Close"])
    res["price_vs_vwap"] = np.where(res["vwap"] != 0, (res["Close"] - res["vwap"]) / res["vwap"], 0.0)
    res["fired"] = (~res["sqz_on"]) & (res["sqz_on"].shift(1) == True)

    res["mom_prev"] = res["momentum"].shift(1).fillna(0)

    def get_mom_state(row):
        m, p = row["momentum"], row["mom_prev"]
        if m > 0:
            return 3 if m >= p else 2
        return 0 if m <= p else 1

    res["mom_state"] = res.apply(get_mom_state, axis=1)

    res["ema_fast"] = _ema(res["Close"], ema_fast)
    res["ema_slow"] = _ema(res["Close"], ema_slow)
    res["ema_filter"] = _ema(res["Close"], 60)
    res["ema_macro"] = _ema(res["Close"], ema_macro)
    res["bullish_align"] = res["ema_fast"] > res["ema_slow"]
    res["bearish_align"] = res["ema_fast"] < res["ema_slow"]

    res["recent_high"] = res["Close"].rolling(window=lookback).max()
    res["recent_low"] = res["Close"].rolling(window=lookback).min()
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

    res["day_open"] = res.groupby("date")["Open"].transform("first")
    res["day_min"] = res.groupby("date")["Low"].cummin()
    res["day_max"] = res.groupby("date")["High"].cummax()
    res["opening_bullish"] = (res["Close"] > res["day_open"]) & (res["day_min"] >= res["day_open"] * 0.999)
    res["opening_bearish"] = (res["Close"] < res["day_open"]) & (res["day_max"] <= res["day_open"] * 1.001)

    return res


def calculate_mtf_alignment(data_dict: dict[str, pd.DataFrame], weights=None) -> dict:
    if not data_dict:
        return {"score": 0, "is_aligned": False}
    if weights is None:
        weights = {"1h": 0.2, "15m": 0.4, "5m": 0.4}
    latest_states = {}
    for tf, df in data_dict.items():
        if df.empty:
            continue
        last = df.iloc[-1]
        momentum = last.get("momentum", 0) if hasattr(last, "get") else (last["momentum"] if "momentum" in last.index else 0)
        if pd.isna(momentum):
            momentum = 0
        direction = 1 if momentum > 0 else -1
        mom_state = last.get("mom_state", 1) if hasattr(last, "get") else (last["mom_state"] if "mom_state" in last.index else 1)
        strength = 1.5 if (mom_state in [0, 3]) else 1.0
        latest_states[tf] = direction * strength
    total_score = 0
    available_weight = 0
    for tf, val in latest_states.items():
        w = weights.get(tf, 0.1)
        total_score += val * w
        available_weight += w
    return {"score": (total_score / (1.5 * available_weight)) * 100 if available_weight > 0 else 0}
