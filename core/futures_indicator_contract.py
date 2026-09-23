"""Stable on-disk/display contract for futures 5m indicator rows."""

CORE_COLUMNS = (
    "timestamp",
    "near_bid", "near_ask", "near_bidask_age_ms", "near_entry_bbo_state",
    "far_bid", "far_ask", "far_bidask_age_ms", "far_entry_bbo_state",
    "close", "spread", "spread_z",
    "sqz_on", "momentum", "atr_used", "vwap", "regime", "session",
)

REST_COLUMNS = (
    "Close", "High", "Low", "Open", "Volume", "amount",
    "atr", "atr_floor", "atr_raw",
    "bb_low", "bb_lower", "bb_mid", "bb_up", "bb_upper",
    "bear_align", "bear_breakout", "bear_breakout_strength",
    "bear_breakout_strength_atr", "bearish_align", "breakout_strength",
    "breakout_strength_atr", "bull_align", "bull_breakout", "bullish_align",
    "d_val", "day_max", "day_min", "day_open", "ema_200_up", "ema_fast",
    "ema_filter", "ema_macro", "ema_slow", "fired", "high", "high_20_prev",
    "in_bear_pb_zone", "in_bull_pb_zone", "in_pb_zone",
    "intraday_strength_pct", "is_bear_structural_breakout",
    "is_bull_structural_breakout", "is_new_high", "is_new_low",
    "is_structural_breakout", "k_val", "low", "low_20_prev", "macd_hist",
    "macd_line", "macd_rising", "macd_signal", "mom_prev", "mom_state",
    "mom_velo", "open", "opening_bearish", "opening_bullish",
    "price_vs_vwap", "price_vs_vwap_pct", "recent_high", "recent_low",
    "router_bias", "router_regime", "rsi", "rsv", "score",
    "squeeze_release", "trading_day", "trend_strength_raw", "volume",
    "volume_spike",
)

FUTURES_INDICATOR_COLUMNS = CORE_COLUMNS + REST_COLUMNS


def ordered_indicator_columns(available):
    """Return contract order while preserving unknown diagnostic columns last."""
    available_set = set(available)
    ordered = [name for name in FUTURES_INDICATOR_COLUMNS if name in available_set]
    ordered.extend(name for name in available if name not in available_set.intersection(FUTURES_INDICATOR_COLUMNS))
    return ordered
