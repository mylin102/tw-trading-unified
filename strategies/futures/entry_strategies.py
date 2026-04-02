"""
Pluggable entry strategies for FuturesMonitor.
Each strategy receives market state and returns a signal dict or None.

Signal format: {"action": "BUY"|"SELL", "reason": str, "stop_loss": float}
"""
import numpy as np


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
        if last_5m.get("bullish_align", False): can_short = False
        if last_5m.get("bearish_align", False): can_long = False
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
    Wider ATR stop (3x), no VWAP exit. Designed for trending nights like 20260402.
    """
    s = cfg.get("strategy", {}).get("trend_follow", {})
    min_score = s.get("min_score", 30)
    atr_mult = s.get("atr_mult", 3.0)

    last_5m, last_15m, score = state["last_5m"], state["last_15m"], state["score"]
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 60

    # Must align with 15m EMA direction
    ema_bullish = last_15m["Close"] > last_15m.get("ema_filter", last_15m["Close"])
    ema_bearish = last_15m["Close"] < last_15m.get("ema_filter", last_15m["Close"])

    # Require strong momentum + EMA alignment + not in squeeze
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
    Pure momentum: enter on squeeze fire with strong velocity.
    No regime filter, just raw momentum burst.
    """
    s = cfg.get("strategy", {}).get("momentum_burst", {})
    min_velo = s.get("min_velocity", 5.0)
    atr_mult = s.get("atr_mult", 2.0)

    last_5m = state["last_5m"]
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 40
    fired = last_5m.get("fired", False)
    mom_velo = last_5m.get("mom_velo", 0)

    if fired and abs(mom_velo) >= min_velo:
        if mom_velo > 0:
            return {"action": "BUY", "reason": "MOM_BURST", "stop_loss": sl}
        else:
            return {"action": "SELL", "reason": "MOM_BURST", "stop_loss": sl}
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

    # Only active during night session
    if not (hour >= 15 or hour < 5):
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

    # Long: green→red→red with high volume, price > SMA
    if bar3_green and bar2_red and bar1_red:
        if vol_bar1 > vol_bar3 * vol_mult and vol_bar2 > vol_bar3 * vol_mult:
            if c[-1] > sma[-1]:
                return {"action": "BUY", "reason": "VOL_REVERSAL", "stop_loss": sl}

    # Short mirror: red→green→green with high volume, price < SMA
    bar3_red = c[-4] < o[-4]
    bar2_green = c[-3] > o[-3]
    bar1_green = c[-2] > o[-2]
    if bar3_red and bar2_green and bar1_green:
        if vol_bar1 > vol_bar3 * vol_mult and vol_bar2 > vol_bar3 * vol_mult:
            if c[-1] < sma[-1]:
                return {"action": "SELL", "reason": "VOL_REVERSAL", "stop_loss": sl}
    return None


def strategy_psar_breakout(state, cfg):
    """
    Ref: r-yabyab/Custom-NinjaScript-Files PSAR strat
    Price just crossed above Parabolic SAR + above SMA50 → long.
    Price just crossed below PSAR + below SMA50 → short.
    """
    s = cfg.get("strategy", {}).get("psar_breakout", {})
    sma_len = s.get("sma_length", 50)
    accel = s.get("acceleration", 0.02)
    accel_max = s.get("acceleration_max", 0.2)
    atr_mult = s.get("atr_mult", 2.0)

    df = state["df_5m"]
    if len(df) < sma_len + 2:
        return None

    last_5m = state["last_5m"]
    atr = last_5m.get("atr", 0)
    sl = atr * atr_mult if atr > 0 else 40

    try:
        psar = df.ta.psar(af0=accel, af=accel, max_af=accel_max)
        # psar returns DataFrame with PSARl (long) and PSARs (short)
        psar_long = psar.iloc[-1, 0]  # PSARl
        psar_short = psar.iloc[-1, 1]  # PSARs
        psar_long_prev = psar.iloc[-2, 0]
        psar_short_prev = psar.iloc[-2, 1]
    except Exception:
        return None

    close = df["Close"].values
    sma = df["Close"].rolling(sma_len).mean().values[-1]
    price = close[-1]
    prev_price = close[-2]

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

    # Approximate cumulative delta: +vol if green bar, -vol if red
    delta = np.where(c > o, v, np.where(c < o, -v, 0))
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


# ── Registry ──
STRATEGIES = {
    "squeeze_breakout": strategy_squeeze_breakout,
    "trend_follow": strategy_trend_follow,
    "vwap_bounce": strategy_vwap_bounce,
    "momentum_burst": strategy_momentum_burst,
    "night_short_only": strategy_night_short_only,
    "volume_reversal": strategy_volume_reversal,
    "psar_breakout": strategy_psar_breakout,
    "cumulative_delta": strategy_cumulative_delta,
}


def get_strategy(name):
    return STRATEGIES.get(name)
