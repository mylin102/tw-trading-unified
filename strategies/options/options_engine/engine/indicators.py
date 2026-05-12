import os

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")

import numpy as np
import pandas as pd
import pandas_ta  # noqa: F401 — registers .ta accessor for df.ta.macd(), df.ta.stoch(), etc.


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
    
    # ── GSD: Always calculate trading_day first (essential for logs/dashboard) ──
    from core.date_utils import get_trading_day
    df = df.copy()
    if not df.empty:
        df["trading_day"] = get_trading_day(df.index)

    if df.empty or len(df) < min_req:
        # GSD: Ensure columns exist even if empty/short
        res = df.copy()
        for col in ["sqz_on", "momentum", "mom_prev", "vwap", "price_vs_vwap", "fired", "mom_state", 
                    "ema_fast", "ema_slow", "ema_filter", "ema_macro", "ema_200_up", "bullish_align", "bearish_align",
                    "recent_high", "recent_low", "is_new_high", "is_new_low", "in_bull_pb_zone", "in_bear_pb_zone",
                    "day_open", "day_min", "day_max", "opening_bullish", "opening_bearish"]:
            if col not in res.columns:
                if col in ["sqz_on", "fired", "bullish_align", "bearish_align", "is_new_high", "is_new_low", "in_bull_pb_zone", "in_bear_pb_zone", "opening_bullish", "opening_bearish"]:
                    res[col] = False
                elif col == "mom_state":
                    res[col] = 0
                else:
                    res[col] = np.nan
        return res

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

    # ═══ [Live Volume Spike] 即時計算，不依賴 Parquet/CSV 舊欄位 ═══
    _vol_ma20 = res["Volume"].rolling(20, min_periods=5).mean().ffill().fillna(res["Volume"])
    res["volume_spike"] = (res["Volume"] / _vol_ma20.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    if not getattr(calculate_futures_squeeze, "_opt_vol_spike_logged", False):
        _last = res.iloc[-1] if len(res) > 0 else None
        if _last is not None:
            print(f"[OptLiveVol] spike={_last.get('volume_spike','?'):.2f} source=live", flush=True)
        calculate_futures_squeeze._opt_vol_spike_logged = True

    res["fired"] = (~res["sqz_on"]) & (res["sqz_on"].shift(1))

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

    # 200日趨勢判斷 (for stock entry strategies)
    res["ema_200_up"] = (res["Close"] > res["ema_macro"]) & (res["ema_macro"] > res["ema_macro"].shift(1))

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

    # [Fix] breakout_strength — safe via pd.to_numeric + .to_numpy(dtype=float)
    if len(res) >= 2:
        try:
            _idx = res.index
            _c = pd.to_numeric(res["Close"], errors="coerce") if "Close" in res.columns else pd.Series(0.0, index=_idx)
            _do = pd.to_numeric(res["day_open"], errors="coerce") if "day_open" in res.columns else pd.Series(np.nan, index=_idx)
            _do = _do.replace(0, np.nan)
            _ir = ((_c - _do) / _do).replace([np.inf, -np.inf], np.nan).fillna(0).reindex(res.index).fillna(0)
            _vb = pd.to_numeric(res["price_vs_vwap"], errors="coerce") if "price_vs_vwap" in res.columns else pd.Series(0.0, index=_idx)
            _vb = _vb.replace([np.inf, -np.inf], np.nan).fillna(0).reindex(res.index).fillna(0)
            _combined = _ir.combine(_vb, max, fill_value=0.0)
            res["breakout_strength"] = _combined * 100.0
        except Exception as e:
            if not getattr(calculate_futures_squeeze, "_warned_breakout", False):
                print(f"[Indicators] breakout_strength failed: {e}", flush=True)
                calculate_futures_squeeze._warned_breakout = True
            res["breakout_strength"] = 0.0
    else:
        res["breakout_strength"] = 0.0
    try:
        if len(res) >= 2 and "ema_fast" in res.columns:
            _ef = pd.to_numeric(res["ema_fast"], errors="coerce") if "ema_fast" in res.columns else pd.Series(np.nan, index=res.index)
            _ep = _ef.shift(1).replace(0, np.nan)
            _es = ((_ef - _ep) / _ep).replace([np.inf, -np.inf], np.nan).fillna(0)
            res["trend_strength_raw"] = pd.Series(_es, index=res.index).to_numpy(dtype=float) * 100
        else:
            res["trend_strength_raw"] = 0.0
    except Exception as e:
        if not getattr(calculate_futures_squeeze, "_warned_trend_strength", False):
            print(f"[Indicators] trend_strength_raw failed: {e}", flush=True)
            calculate_futures_squeeze._warned_trend_strength = True
        res["trend_strength_raw"] = 0.0

    return res


def calculate_stock_squeeze(
    df: pd.DataFrame,
    bb_length=14,
    bb_std=2.0,
    kc_length=14,
    kc_scalar=1.5,
    ema_fast=20,
    ema_slow=60,
    ema_macro=200,
    macd_fast=12,
    macd_slow=26,
    macd_signal=9,
    kd_length=9,
    adx_length=14,
) -> pd.DataFrame:
    """
    Stock-specific squeeze indicator with MACD, KD, and ADX.
    Wraps calculate_futures_squeeze and adds extra indicators needed by stock strategies.
    """
    res = calculate_futures_squeeze(
        df, bb_length=bb_length, bb_std=bb_std, kc_length=kc_length,
        kc_scalar=kc_scalar, ema_fast=ema_fast, ema_slow=ema_slow,
        lookback=60, pb_buffer=1.002, ema_macro=ema_macro,
    )

    # GSD: Ensure technical indicator columns exist even if DataFrame is short
    # Add missing technical indicator columns with NaN values
    tech_indicator_cols = [
        "macd",
        "macd_signal",
        "macd_hist",
        "macd_rising",
        "k_val",
        "d_val",
        "adx",
        "dmp",
        "dmn",
        "bb_lower",
        "bb_mid",
        "bb_upper",
        "money_flow_multiplier",
        "bar_delta",
        "cum_bar_delta",
        "delta_trend",
        "vwap_std",
        "z_vwap",
        "vwap_upper_1",
        "vwap_lower_1",
        "vwap_upper_2",
        "vwap_lower_2",
    ]
    
    for col in tech_indicator_cols:
        if col not in res.columns:
            res[col] = np.nan

    close = pd.to_numeric(res["Close"], errors="coerce").fillna(0.0)
    high = pd.to_numeric(res["High"], errors="coerce").fillna(0.0)
    low = pd.to_numeric(res["Low"], errors="coerce").fillna(0.0)
    volume = pd.to_numeric(res["Volume"], errors="coerce").fillna(0.0)

    price_range = (high - low).replace(0, np.nan)
    money_flow_multiplier = (((close - low) - (high - close)) / price_range).clip(-1, 1).fillna(0.0)
    res["money_flow_multiplier"] = money_flow_multiplier
    res["bar_delta"] = money_flow_multiplier * volume
    res["cum_bar_delta"] = res.groupby("trading_day")["bar_delta"].cumsum()
    res["delta_trend"] = res.groupby("trading_day")["cum_bar_delta"].diff(5).fillna(0.0)

    vwap_deviation = close - pd.to_numeric(res["vwap"], errors="coerce").fillna(close)
    res["vwap_std"] = (
        vwap_deviation.groupby(res["trading_day"]).expanding().std(ddof=0).reset_index(level=0, drop=True).fillna(0.0)
    )
    res["z_vwap"] = np.where(res["vwap_std"] > 0, vwap_deviation / res["vwap_std"], 0.0)
    res["vwap_upper_1"] = res["vwap"] + res["vwap_std"]
    res["vwap_lower_1"] = res["vwap"] - res["vwap_std"]
    res["vwap_upper_2"] = res["vwap"] + (2 * res["vwap_std"])
    res["vwap_lower_2"] = res["vwap"] - (2 * res["vwap_std"])
    
    if len(df) < max(macd_slow, adx_length, kd_length):
        return res

    # MACD histogram
    macd = df.ta.macd(close="Close", fast=macd_fast, slow=macd_slow, signal=macd_signal)
    if macd is not None:
        macd_cols = [c for c in macd.columns]
        if len(macd_cols) >= 3:
            res["macd"] = macd[macd_cols[0]]
            res["macd_signal"] = macd[macd_cols[1]]
            res["macd_hist"] = macd[macd_cols[2]]
            res["macd_rising"] = res["macd_hist"] > res["macd_hist"].shift(1)

    # KD (Stochastic Oscillator)
    stoch = df.ta.stoch(close="Close", high="High", low="Low", k=kd_length, d=kd_length, smooth_k=3)
    if stoch is not None:
        stoch_cols = [c for c in stoch.columns]
        if len(stoch_cols) >= 2:
            res["k_val"] = stoch[stoch_cols[0]]
            res["d_val"] = stoch[stoch_cols[1]]

    # ADX
    adx = df.ta.adx(high="High", low="Low", close="Close", length=adx_length)
    if adx is not None:
        adx_cols = [c for c in adx.columns]
        if len(adx_cols) >= 1:
            res["adx"] = adx[adx_cols[0]]
            if len(adx_cols) >= 3:
                res["dmp"] = adx[adx_cols[1]]
                res["dmn"] = adx[adx_cols[2]]

    # BB lower band (for mean reversion strategies)
    bb = df.ta.bbands(close="Close", length=bb_length, std=bb_std)
    if bb is not None:
        bb_cols = [c for c in bb.columns]
        if len(bb_cols) >= 3:
            res["bb_lower"] = bb[bb_cols[0]]
            res["bb_mid"] = bb[bb_cols[1]]
            res["bb_upper"] = bb[bb_cols[2]]

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
