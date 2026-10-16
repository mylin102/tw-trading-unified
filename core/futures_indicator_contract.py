"""Stable on-disk/display contract for futures 5m indicator rows."""

CORE_COLUMNS = (
    "timestamp",
    "near_bid", "near_ask", "near_bidask_age_ms", "near_entry_bbo_state",
    "far_bid", "far_ask", "far_bidask_age_ms", "far_entry_bbo_state",
    "close", "open", "high", "low", "volume", "spread", "spread_z",
    "sqz_on", "momentum", "atr_used", "vwap", "regime", "session",
)

REST_COLUMNS = (
    "atr", "atr_raw",
    "bb_low", "bb_mid", "bb_up",
    "bearish_align", "bullish_align",
    "bear_breakout", "bear_breakout_strength_atr", "bull_breakout",
    "day_max", "day_min",
    "ema_fast", "ema_slow",
    "high_20_prev", "low_20_prev",
    "macd_hist", "mom_velo", "price_vs_vwap", "rsi", "score",
    "trend_strength_raw",
)

FUTURES_INDICATOR_COLUMNS = CORE_COLUMNS + REST_COLUMNS


def ordered_indicator_columns(available):
    """Return only the fixed contract columns in canonical order."""
    available_set = set(available)
    return [name for name in FUTURES_INDICATOR_COLUMNS if name in available_set]
