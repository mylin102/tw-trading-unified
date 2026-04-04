"""
Pluggable entry strategies for FuturesMonitor.
Each strategy receives market state and returns a signal dict or None.

Signal format: {"action": "BUY"|"SELL", "reason": str, "stop_loss": float}
"""
import numpy as np
import pandas as pd


def strategy_squeeze_breakout(state, cfg):
    """Original: Squeeze release + trend alignment + regime filter."""
    s = cfg.get("strategy", {})
    entry_score = s.get("entry_score", 20)
    filter_mode = s.get("regime_filter", "mid")

    last_5m, last_15m, score = state["last_5m"], state["last_15m"], state["score"]
    sqz_buy = (not last_5m["sqz_on"]) and score >= entry_score and last_5m["mom_state"] >= 2
    sqz_sell = (not last_5m["sqz_on"]) and score <= -entry_score and last_5m["mom_state"] <= 1

    if filter_mode == "loose":
        can_long = can_short = True
    elif filter_mode == "mid":
        can_long = last_15m["Close"] > last_15m["ema_filter"] * 0.998
        can_short = last_15m["Close"] < last_15m["ema_filter"] * 1.002
        if last_5m.get("bullish_align", False):
            can_short = False
        if last_5m.get("bearish_align", False):
            can_long = False
    else:
        can_long = last_15m["Close"] > last_15m["ema_filter"] * 0.999
        can_short = last_15m["Close"] < last_15m["ema_filter"] * 1.001

    trend = state.get("trend", {})
    if sqz_buy and can_long and trend.get("trend_long"):
        return {"action": "BUY", "reason": "SYNERGY", "stop_loss": state["stop_loss_pts"]}
    if sqz_buy and can_long:
        return {"action": "BUY", "reason": "SQUEEZE", "stop_loss": state["stop_loss_pts"]}
    if sqz_sell and can_short and trend.get("trend_short"):
        return {"action": "SELL", "reason": "SYNERGY", "stop_loss": state["stop_loss_pts"]}
    if sqz_sell and can_short:
        return {"action": "SELL", "reason": "SQUEEZE", "stop_loss": state["stop_loss_pts"]}
    return None


def strategy_trend_follow(state, cfg):
    """
    Trend-following: only trade in direction of higher-timeframe EMA.
    Wider ATR stop (3x), trailing ATR exit on reversal.
    """
    s = cfg.get("strategy", {}).get("trend_follow", {})
    min_score = s.get("min_score", 30)
    atr_mult = s.get("atr_mult", 3.0)
    s.get("trailing_atr", 2.0)

    last_5m, last_15m, score = state["last_5m"], state["last_15m"], state["score"]
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 60

    ema_bullish = last_15m["Close"] > last_15m.get("ema_filter", last_15m["Close"])
    ema_bearish = last_15m["Close"] < last_15m.get("ema_filter", last_15m["Close"])

    if not last_5m["sqz_on"] and score >= min_score and ema_bullish and last_5m.get("bullish_align"):
        return {"action": "BUY", "reason": "TREND_FOLLOW", "stop_loss": sl}
    if not last_5m["sqz_on"] and score <= -min_score and ema_bearish and last_5m.get("bearish_align"):
        return {"action": "SELL", "reason": "TREND_FOLLOW", "stop_loss": sl}
    return None


def strategy_vwap_bounce(state, cfg):
    """
    Mean-reversion: fade moves away from VWAP when momentum weakens.
    Tight stop, quick profit target.
    """
    s = cfg.get("strategy", {}).get("vwap_bounce", {})
    dist_pct = s.get("min_distance_pct", 0.003)  # 0.3% away from VWAP
    atr_mult = s.get("atr_mult", 1.5)

    last_5m = state["last_5m"]
    price = last_5m["Close"]
    vwap = last_5m["vwap"]
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 30
    mom_state = last_5m["mom_state"]

    if vwap <= 0:
        return None

    dist = (price - vwap) / vwap

    # Price far below VWAP + momentum turning up → buy bounce
    if dist < -dist_pct and mom_state >= 1 and last_5m.get("sqz_on"):
        return {"action": "BUY", "reason": "VWAP_BOUNCE", "stop_loss": sl}
    # Price far above VWAP + momentum turning down → sell bounce
    if dist > dist_pct and mom_state <= 2 and last_5m.get("sqz_on"):
        return {"action": "SELL", "reason": "VWAP_BOUNCE", "stop_loss": sl}
    return None


def strategy_momentum_burst(state, cfg):
    """
    Pure momentum: enter on squeeze fire with extreme velocity (Z-score normalized).
    """
    s = cfg.get("strategy", {}).get("momentum_burst", {})
    min_zscore = s.get("min_zscore", 2.0)
    atr_mult = s.get("atr_mult", 2.0)

    last_5m = state["last_5m"]
    df = state.get("df_5m")
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 40
    fired = last_5m.get("fired", False)
    mom_velo = last_5m.get("mom_velo", 0)

    if not fired or df is None or len(df) < 30:
        return None

    # Z-score normalization
    velo_series = df["mom_velo"] if "mom_velo" in df.columns else None
    if velo_series is None:
        return None
    mean = velo_series.iloc[-100:].mean() if len(velo_series) >= 100 else velo_series.mean()
    std = velo_series.iloc[-100:].std() if len(velo_series) >= 100 else velo_series.std()
    if std < 1e-8:
        return None
    zscore = (mom_velo - mean) / std

    if abs(zscore) >= min_zscore:
        action = "BUY" if zscore > 0 else "SELL"
        return {"action": action, "reason": "MOM_BURST", "stop_loss": sl}
    return None


def strategy_night_short_only(state, cfg):
    """
    Night session bias: only short during night (15:00~05:00).
    Uses squeeze + bearish alignment. Designed for overnight gap-down tendency.
    """
    s = cfg.get("strategy", {}).get("night_short", {})
    min_score = s.get("min_score", 20)
    atr_mult = s.get("atr_mult", 2.0)

    last_5m = state["last_5m"]
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 50
    score = state["score"]
    hour = state["hour"]

    # Only active during night session, stop 30 min before close
    if not (hour >= 15 or hour < 4):
        return None

    if not last_5m["sqz_on"] and score <= -min_score and last_5m.get("bearish_align"):
        return {"action": "SELL", "reason": "NIGHT_SHORT", "stop_loss": sl}
    return None


def strategy_volume_reversal(state, cfg):
    """
    Ref: r-yabyab/Custom-NinjaScript-Files volumeMA
    2 consecutive red bars with volume > green bar volume * multiplier,
    preceded by a green bar. Price above SMA → long reversal.
    Mirror logic for short.
    """
    s = cfg.get("strategy", {}).get("volume_reversal", {})
    sma_len = s.get("sma_length", 50)
    vol_mult = s.get("volume_multiplier", 2.0)
    atr_mult = s.get("atr_mult", 2.0)

    df = state["df_5m"]
    if len(df) < sma_len + 4:
        return None

    last_5m = state["last_5m"]
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 40

    c = df["Close"].values
    o = df["Open"].values
    v = df["Volume"].values
    sma = df["Close"].rolling(sma_len).mean().values

    # bars: [3]=green, [2]=red, [1]=red, [0]=current
    bar3_green = c[-4] > o[-4]
    bar2_red = c[-3] < o[-3]
    bar1_red = c[-2] < o[-2]
    vol_bar1 = v[-2]
    vol_bar2 = v[-3]
    vol_bar3 = v[-4]

    # Long: green→red→red with high volume vs 20-bar MA, price > SMA
    vol_ma = df["Volume"].rolling(20).mean().values[-1] if len(df) >= 20 else vol_bar3
    if bar3_green and bar2_red and bar1_red:
        if vol_bar1 > vol_ma * vol_mult and vol_bar2 > vol_ma * vol_mult:
            if c[-1] > sma[-1]:
                return {"action": "BUY", "reason": "VOL_REVERSAL", "stop_loss": sl}

    # Short mirror: red→green→green with high volume, price < SMA
    bar3_red = c[-4] < o[-4]
    bar2_green = c[-3] > o[-3]
    bar1_green = c[-2] > o[-2]
    if bar3_red and bar2_green and bar1_green:
        if vol_bar1 > vol_ma * vol_mult and vol_bar2 > vol_ma * vol_mult:
            if c[-1] < sma[-1]:
                return {"action": "SELL", "reason": "VOL_REVERSAL", "stop_loss": sl}
    return None


def strategy_psar_breakout(state, cfg):
    """
    PSAR 突破。結合拋物線指標轉向與 50MA 過濾。
    """
    s = cfg.get("strategy", {}).get("psar_breakout", {})
    sma_len = s.get("sma_length", 50)
    accel = s.get("acceleration", 0.02)
    accel_max = s.get("acceleration_max", 0.2)
    atr_mult = s.get("atr_mult", 2.0)

    # Use the full dataframe passed in state (we need to inject it from signal_generator)
    # Alternatively, since state["df_5m"] is a slice, we can cache the calculation on the parent df.
    df = state.get("df_5m_full")
    if df is None:
        df = state["df_5m"] # fallback to slice if full not available
        
    if len(df) < sma_len + 2:
        return None

    # Optimization: Calculate PSAR once for the whole DF and cache it in cfg or df.attrs
    cache_key = f"psar_{accel}_{accel_max}"
    if cache_key not in df.attrs:
        try:
            # df.ta.psar returns a DataFrame with columns like 'PSARl_0.02_0.2', 'PSARs_0.02_0.2', 'PSARaf_0.02_0.2', 'PSARr_0.02_0.2'
            # The column names contain the parameters.
            psar_df = df.ta.psar(af0=accel, af=accel, max_af=accel_max)
            df.attrs[cache_key] = psar_df
        except Exception:
            return None
            
    psar = df.attrs[cache_key]
    
    # Now we need the values for the CURRENT index
    # We are evaluating at the last row of the current slice (state["df_5m"])
    current_time = state["last_5m"].name
    
    try:
        # Get the row in the full PSAR dataframe corresponding to the current time
        # For speed, we just use the position. If state has 'idx', use that.
        idx = state.get("idx")
        if idx is not None and idx >= 1:
            psar_long = psar.iloc[idx, 0]
            psar_short = psar.iloc[idx, 1]
            psar_long_prev = psar.iloc[idx - 1, 0]
            psar_short_prev = psar.iloc[idx - 1, 1]
        else:
            # Fallback to matching by index label (slower but safe)
            current_idx_loc = psar.index.get_loc(current_time)
            psar_long = psar.iloc[current_idx_loc, 0]
            psar_short = psar.iloc[current_idx_loc, 1]
            psar_long_prev = psar.iloc[current_idx_loc - 1, 0]
            psar_short_prev = psar.iloc[current_idx_loc - 1, 1]
    except Exception:
        return None

    last_5m = state["last_5m"]
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 40

    # Ensure SMA is pre-calculated or calculate efficiently
    sma_col = f"sma_{sma_len}"
    if sma_col not in df.columns:
        df[sma_col] = df["Close"].rolling(sma_len).mean()
        
    price = last_5m["Close"]
    
    if idx is not None:
        sma = df[sma_col].values[idx]
    else:
        sma = df[sma_col].loc[current_time]
        
    if pd.isna(sma):
        return None

    # Adaptive ADX Filter: Lowered to 15 to catch trend starts (V-Model v2)
    adx = last_5m.get("adx", 25)
    if adx < 15:
        return None

    # Adaptive Stop: If ATR is high (volatile), use tighter mult
    current_vol_mult = 1.5 if atr > 40 else atr_mult
    sl = atr * current_vol_mult
    
    import math
    # Long: price crosses above PSAR + above SMA
    if not math.isnan(psar_long) and math.isnan(psar_long_prev):
        # PSAR flipped to long
        if price > sma:
            return {"action": "BUY", "reason": "PSAR_BREAKOUT", "stop_loss": sl}

    # Short: PSAR flipped to short + below SMA
    if not math.isnan(psar_short) and math.isnan(psar_short_prev):
        if price < sma:
            return {"action": "SELL", "reason": "PSAR_BREAKOUT", "stop_loss": sl}
    return None


def strategy_cumulative_delta(state, cfg):
    """
    Ref: r-yabyab/Custom-NinjaScript-Files cumStrat
    Approximation using volume + price direction as delta proxy.
    Cumulative delta rising + price above SMA + price pulled back → long.
    """
    s = cfg.get("strategy", {}).get("cumulative_delta", {})
    sma_len = s.get("sma_length", 50)
    lookback = s.get("lookback", 20)
    atr_mult = s.get("atr_mult", 2.0)

    df = state["df_5m"]
    if len(df) < max(sma_len, lookback) + 2:
        return None

    last_5m = state["last_5m"]
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 40

    c = df["Close"].values
    o = df["Open"].values
    v = df["Volume"].values

    # Price-weighted cumulative delta: larger price moves contribute more
    price_change = np.where(o != 0, (c - o) / o, 0)
    delta = price_change * v
    cum_delta = np.cumsum(delta)

    sma = df["Close"].rolling(sma_len).mean().values[-1]
    price = c[-1]
    cd_now = cum_delta[-1]
    cd_past = cum_delta[-lookback] if len(cum_delta) > lookback else cum_delta[0]

    # Long: cum delta rising + price > SMA + price pulled back from lookback ago
    if cd_now > cd_past and price > sma and price < c[-lookback]:
        return {"action": "BUY", "reason": "CUM_DELTA", "stop_loss": sl}

    # Short: cum delta falling + price < SMA + price bounced from lookback ago
    if cd_now < cd_past and price < sma and price > c[-lookback]:
        return {"action": "SELL", "reason": "CUM_DELTA", "stop_loss": sl}
    return None


def strategy_vol_squeeze(state, cfg):
    """
    Enhanced Squeeze: Original logic + Volume Spike filter.
    Ensures that the breakout has institutional participation.
    """
    s = cfg.get("strategy", {})
    entry_score = s.get("entry_score", 20)
    vol_mult = s.get("vol_multiplier", 1.5)

    last_5m, last_15m, score = state["last_5m"], state["last_15m"], state["score"]
    df = state.get("df_5m")
    
    if df is None or len(df) < 20:
        return None

    # Volume filter: Current volume > SMA(Volume, 20) * multiplier
    vol_ma = df["Volume"].rolling(20).mean().iloc[-1]
    curr_vol = last_5m["Volume"]
    vol_spike = curr_vol > vol_ma * vol_mult

    sqz_buy = (not last_5m["sqz_on"]) and score >= entry_score and last_5m["mom_state"] >= 2 and vol_spike
    sqz_sell = (not last_5m["sqz_on"]) and score <= -entry_score and last_5m["mom_state"] <= 1 and vol_spike

    # Use mid-regime filtering logic
    can_long = last_15m["Close"] > last_15m["ema_filter"] * 0.998
    can_short = last_15m["Close"] < last_15m["ema_filter"] * 1.002

    if sqz_buy and can_long:
        return {"action": "BUY", "reason": "VOL_SQZ", "stop_loss": state["stop_loss_pts"]}
    if sqz_sell and can_short:
        return {"action": "SELL", "reason": "VOL_SQZ", "stop_loss": state["stop_loss_pts"]}
    return None


def strategy_gap_reversal(state, cfg):
    """
    Taiwan Specific: Gap Reversal.
    Captures over-reaction after a large opening gap (Day or Night).
    If Gap > 100 pts and first bars show reversal momentum -> Enter fade.
    """
    s = cfg.get("strategy", {}).get("gap_reversal", {})
    min_gap = s.get("min_gap_pts", 80)
    atr_mult = s.get("atr_mult", 1.5)

    last_5m = state["last_5m"]
    df = state.get("df_5m")
    if df is None or len(df) < 5:
        return None

    # Get the trading day open price
    day_open = last_5m.get("day_open")
    if day_open is None or day_open <= 0:
        return None

    # Approximate previous close (last bar of previous trading day)
    # This is a simplification; in a real engine we'd cache the actual last close.
    # Here we look for the first bar of the trading day and compare with the bar before it.
    curr_td = last_5m.get("trading_day")
    
    # Check if we are in the first 30 minutes of the session
    # We only trade gap reversals early in the day
    session_start_idx = np.where(df["trading_day"] == curr_td)[0]
    if len(session_start_idx) == 0:
        return None
    
    first_idx = session_start_idx[0]
    curr_idx = len(df) - 1
    
    # Only active within first 6 bars (30 mins) of the trading day
    if not (0 <= (curr_idx - first_idx) <= 6):
        return None

    # Calculate gap (Open of first bar - Close of bar before it)
    if first_idx <= 0:
        return None
        
    prev_close = df["Close"].iloc[first_idx - 1]
    actual_open = df["Open"].iloc[first_idx]
    gap = actual_open - prev_close

    atr = last_5m.get("atr", 30)
    sl = atr * atr_mult

    # Long: Large Gap Down (>80) + Price starts rising above opening
    if gap < -min_gap and last_5m["Close"] > actual_open and last_5m["mom_state"] >= 2:
        return {"action": "BUY", "reason": "GAP_REVERSAL", "stop_loss": sl}
        
    # Short: Large Gap Up (>80) + Price starts falling below opening
    if gap > min_gap and last_5m["Close"] < actual_open and last_5m["mom_state"] <= 1:
        return {"action": "SELL", "reason": "GAP_REVERSAL", "stop_loss": sl}
        
    return None


# ── Registry ──
STRATEGIES = {
    "squeeze_breakout": {
        "func": strategy_squeeze_breakout,
        "desc": "Squeeze 釋放 + 趨勢對齊。捕捉波動率擠壓後的噴發，搭配 15m 趨勢過濾器，適合趨勢發動初期。"
    },
    "vol_squeeze": {
        "func": strategy_vol_squeeze,
        "desc": "量能過濾 Squeeze。在突破瞬間要求成交量爆發 (預設 1.5x)，過濾假突破，提高信號品質。"
    },
    "gap_reversal": {
        "func": strategy_gap_reversal,
        "desc": "跳空反轉。專為台指期設計，當開盤產生大跳空時，捕捉開盤 30 分鐘內的過度反應收斂點。"
    },
    "trend_follow": {
        "func": strategy_trend_follow,
        "desc": "純趨勢追蹤。僅在 15m EMA 方向一致時進場，使用較寬的 3x ATR 止損，適合強勢多/空頭市場。"
    },
    "vwap_bounce": {
        "func": strategy_vwap_bounce,
        "desc": "均值回歸。當價格偏離 VWAP 過遠 (0.3%+) 且動能轉折時逆勢進場，捕捉乖離過大的回抽。"
    },
    "momentum_burst": {
        "func": strategy_momentum_burst,
        "desc": "動能噴發。監測 Squeeze 觸發瞬間的動能速度 (Z-Score > 2)，追求極速獲利，但風險較高。"
    },
    "night_short_only": {
        "func": strategy_night_short_only,
        "desc": "夜盤偏空策略。專為 15:00~05:00 設計，捕捉夜盤常見的高檔無力回落現象。"
    },
    "volume_reversal": {
        "func": strategy_volume_reversal,
        "desc": "成交量反轉。偵測連續兩根帶量長黑/長紅後的衰竭，在 MA 方向支持下捕捉反彈點。"
    },
    "psar_breakout": {
        "func": strategy_psar_breakout,
        "desc": "PSAR 突破。結合拋物線指標轉向與 50MA 過濾，是 Q1 回測中表現最穩健的趨勢轉向策略。"
    },
    "cumulative_delta": {
        "func": strategy_cumulative_delta,
        "desc": "累計量能差 (估計)。利用成交量與價格變動方向的累計差值，尋找量價背離後的拉回進場點。"
    },
}


def get_strategy(name):
    return STRATEGIES.get(name, {}).get("func")
