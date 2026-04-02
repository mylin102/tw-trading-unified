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
    """計算 ATR (Average True Range)"""
    if df.empty or len(df) < length:
        return pd.Series(0.0, index=df.index)
    return _true_range(df).rolling(window=length, min_periods=length).mean()


def calculate_futures_squeeze(
    df: pd.DataFrame,
    bb_length: int = 20,
    bb_std: float = 2.0,
    kc_length: int = 20,
    kc_scalar: float = 1.5,
    pb_buffer: float = 1.002,
    ema_fast: int = 12,
    ema_slow: int = 36,
    ema_macro: int = 200,
) -> pd.DataFrame:
    """
    計算完整的期貨 Squeeze + 趨勢指標
    """
    if len(df) < bb_length:
        return pd.DataFrame()

    if ta:
        sqz_on, momentum = _pandas_ta_squeeze(df, bb_length, bb_std, kc_length, kc_scalar)
    else:
        sqz_on, momentum = _fallback_squeeze(df, bb_length, bb_std, kc_length, kc_scalar)

    res = df.copy()
    res["sqz_on"] = sqz_on
    res["momentum"] = momentum
    res["atr"] = calculate_atr(df, length=bb_length)
    
    # 計算動能斜率 (Velocity): 3 棒變化量的移動平均
    res["mom_velo"] = res["momentum"].diff(1).rolling(window=3).mean().fillna(0.0)
    
    # 改進 VWAP：使用交易日 (Trading Day) 而非日曆日 (Calendar Day)
    # 台指期規則：15:00 以後屬下一個交易日
    res["trading_day"] = (res.index + pd.Timedelta(hours=9)).date
    
    typical_price_x_volume = res["Close"] * res["Volume"]
    volume_cumsum = res.groupby("trading_day")["Volume"].cumsum()
    res["vwap"] = typical_price_x_volume.groupby(res["trading_day"]).cumsum() / volume_cumsum
    res["vwap"] = res["vwap"].where(volume_cumsum != 0, res["Close"])
    res["price_vs_vwap"] = np.where(res["vwap"] != 0, (res["Close"] - res["vwap"]) / res["vwap"], 0.0)
    res["fired"] = (~res["sqz_on"]) & (res["sqz_on"].shift(1) == True)

    # 向量化 mom_state 計算，提升效能
    res["mom_prev"] = res["momentum"].shift(1).fillna(0)
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

    # 趨勢與過濾器
    res["ema_fast"] = _ema(res["Close"], ema_fast)
    res["ema_slow"] = _ema(res["Close"], ema_slow)
    res["ema_filter"] = _ema(res["Close"], 60)
    res["ema_macro"] = _ema(res["Close"], ema_macro)
    res["bullish_align"] = res["ema_fast"] > res["ema_slow"]
    res["bearish_align"] = res["ema_fast"] < res["ema_slow"]

    # 波動率調整後的 Pullback 區域
    res["recent_high"] = res["High"].rolling(window=bb_length).max()
    res["recent_low"] = res["Low"].rolling(window=bb_length).min()
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

    # 開盤型態
    res["day_open"] = res.groupby("trading_day")["Open"].transform("first")
    res["day_min"] = res.groupby("trading_day")["Low"].cummin()
    res["day_max"] = res.groupby("trading_day")["High"].cummax()
    res["opening_bullish"] = (res["Close"] > res["day_open"]) & (res["day_min"] >= res["day_open"] * 0.999)
    res["opening_bearish"] = (res["Close"] < res["day_open"]) & (res["day_max"] <= res["day_open"] * 1.001)

    return res


def calculate_mtf_alignment(processed_dfs: dict[str, pd.DataFrame], weights: dict[str, float]) -> dict:
    """
    計算多週期對齊分數
    """
    if not processed_dfs:
        return {"score": 0}

    latest_states = {}
    for tf, df in processed_dfs.items():
        if not df.empty:
            # 使用 mom_state (0-3) 作為基準，轉換為 -1.5 到 +1.5
            val = df["mom_state"].iloc[-1]
            latest_states[tf] = val - 1.5

    total_score = 0
    available_weight = 0
    for tf, val in latest_states.items():
        w = weights.get(tf, 0.1)
        total_score += val * w
        available_weight += w
    return {"score": (total_score / (1.5 * available_weight)) * 100 if available_weight > 0 else 0}
