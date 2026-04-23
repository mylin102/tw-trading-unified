"""
strategy_router.py

Strategy routing for a futures auto-trading engine.
This module assumes:
1. one kbar row has already been converted into a dict
2. regime_engine.classify_regime() is called first
3. order execution is handled elsewhere

Design goals:
- one-bar deterministic decision
- one active direction only
- explicit no-trade states
- easy to unit test
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, Optional

from regime_engine import RegimeConfig, RegimeResult, classify_regime


@dataclass(frozen=True)
class RouterConfig:
    breakout_entry_threshold: float = 0.60
    counter_vwap_threshold: float = 0.0020
    hard_block_countertrend_in_trend: bool = True
    min_adx_for_breakout: float = 30.0
    require_volume_spike_for_breakout: bool = True
    allow_reversion_in_weak: bool = True
    allow_pullback_with_macd_recovery: bool = True


@dataclass(frozen=True)
class Signal:
    action: str              # BUY / SELL / FLAT
    strategy: str            # breakout_long, pullback_long, etc.
    confidence: float
    reason: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _b(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return bool(x)
    if isinstance(x, str):
        return x.strip().lower() in {"true", "1", "yes", "y"}
    return False


def _flat(reason: str) -> Signal:
    return Signal(
        action="FLAT",
        strategy="no_trade",
        confidence=0.0,
        reason=reason,
    )


def breakout_long_signal(row: Dict[str, Any], cfg: RouterConfig) -> Optional[Signal]:
    breakout_strength = _f(row.get("breakout_strength"))
    adx = _f(row.get("adx"))
    volume_spike = _f(row.get("volume_spike"))
    is_new_high = _b(row.get("is_new_high"))
    bullish_align = _b(row.get("bull_align")) or _b(row.get("bullish_align"))

    if breakout_strength < cfg.breakout_entry_threshold:
        return None
    if adx < cfg.min_adx_for_breakout:
        return None
    if cfg.require_volume_spike_for_breakout and volume_spike < 1:
        return None
    if not is_new_high:
        return None
    if not bullish_align:
        return None

    close = _f(row.get("close"))
    recent_low = _f(row.get("recent_low"), None)
    atr = max(_f(row.get("atr"), 0.0), 1.0)
    stop = recent_low if recent_low else close - 1.2 * atr
    tp = close + 2.0 * atr

    return Signal(
        action="BUY",
        strategy="breakout_long",
        confidence=0.82,
        reason="trend breakout confirmed by ADX/new high/alignment",
        stop_loss=stop,
        take_profit=tp,
    )


def breakout_short_signal(row: Dict[str, Any], cfg: RouterConfig) -> Optional[Signal]:
    breakout_strength = _f(row.get("breakout_strength"))
    adx = _f(row.get("adx"))
    volume_spike = _f(row.get("volume_spike"))
    is_new_low = _b(row.get("is_new_low"))
    bearish_align = _b(row.get("bear_align")) or _b(row.get("bearish_align"))

    if breakout_strength < cfg.breakout_entry_threshold:
        return None
    if adx < cfg.min_adx_for_breakout:
        return None
    if cfg.require_volume_spike_for_breakout and volume_spike < 1:
        return None
    if not is_new_low:
        return None
    if not bearish_align:
        return None

    close = _f(row.get("close"))
    recent_high = _f(row.get("recent_high"), None)
    atr = max(_f(row.get("atr"), 0.0), 1.0)
    stop = recent_high if recent_high else close + 1.2 * atr
    tp = close - 2.0 * atr

    return Signal(
        action="SELL",
        strategy="breakout_short",
        confidence=0.82,
        reason="trend breakdown confirmed by ADX/new low/alignment",
        stop_loss=stop,
        take_profit=tp,
    )


def weak_pullback_long_signal(row: Dict[str, Any], cfg: RouterConfig) -> Optional[Signal]:
    if not cfg.allow_reversion_in_weak:
        return None

    in_bear_pb_zone = _b(row.get("in_bear_pb_zone"))
    price_vs_vwap = _f(row.get("price_vs_vwap"))
    macd_rising = _b(row.get("macd_rising"))
    mom_state = _f(row.get("mom_state"))
    close = _f(row.get("close"))
    recent_low = _f(row.get("recent_low"), None)
    vwap = _f(row.get("vwap"))
    atr = max(_f(row.get("atr"), 0.0), 1.0)

    if not in_bear_pb_zone:
        return None
    if price_vs_vwap > -cfg.counter_vwap_threshold:
        return None
    if cfg.allow_pullback_with_macd_recovery and not macd_rising:
        return None
    if mom_state < 2:
        return None

    stop = recent_low if recent_low else close - 1.0 * atr
    tp = vwap if vwap else close + 1.5 * atr

    return Signal(
        action="BUY",
        strategy="weak_pullback_long",
        confidence=0.68,
        reason="weak regime bearish pullback with MACD recovery and mean-reversion edge",
        stop_loss=stop,
        take_profit=tp,
    )


def weak_pullback_short_signal(row: Dict[str, Any], cfg: RouterConfig) -> Optional[Signal]:
    if not cfg.allow_reversion_in_weak:
        return None

    in_bull_pb_zone = _b(row.get("in_bull_pb_zone"))
    price_vs_vwap = _f(row.get("price_vs_vwap"))
    macd_rising = _b(row.get("macd_rising"))
    mom_state = _f(row.get("mom_state"))
    close = _f(row.get("close"))
    recent_high = _f(row.get("recent_high"), None)
    vwap = _f(row.get("vwap"))
    atr = max(_f(row.get("atr"), 0.0), 1.0)

    if not in_bull_pb_zone:
        return None
    if price_vs_vwap < cfg.counter_vwap_threshold:
        return None
    # For short reversion, avoid entering if MACD is still rising strongly.
    if macd_rising:
        return None
    if mom_state > -2:
        return None

    stop = recent_high if recent_high else close + 1.0 * atr
    tp = vwap if vwap else close - 1.5 * atr

    return Signal(
        action="SELL",
        strategy="weak_pullback_short",
        confidence=0.68,
        reason="weak regime bullish pullback fading back toward VWAP",
        stop_loss=stop,
        take_profit=tp,
    )


def route_signal(
    row: Dict[str, Any],
    regime_result: Optional[RegimeResult] = None,
    regime_config: Optional[RegimeConfig] = None,
    router_config: Optional[RouterConfig] = None,
) -> Signal:
    """
    Returns exactly one signal.
    Priority:
    1. breakout in TREND
    2. mean reversion in WEAK/STRETCHED
    3. no trade

    This prevents contradictory buy/sell outputs on the same bar.
    """
    rcfg = router_config or RouterConfig()
    regime = regime_result or classify_regime(row, regime_config)

    if regime.regime == "TREND":
        if regime.bias == "LONG":
            sig = breakout_long_signal(row, rcfg)
            if sig:
                return sig
        elif regime.bias == "SHORT":
            sig = breakout_short_signal(row, rcfg)
            if sig:
                return sig

        return _flat("TREND regime detected but no valid breakout trigger")

    if regime.regime in {"WEAK", "STRETCHED"}:
        if regime.bias == "SHORT":
            sig = weak_pullback_long_signal(row, rcfg)
            if sig:
                return sig
        elif regime.bias == "LONG":
            sig = weak_pullback_short_signal(row, rcfg)
            if sig:
                return sig

        return _flat(f"{regime.regime} regime detected but no mean-reversion trigger")

    if regime.regime == "SQUEEZE":
        return _flat("SQUEEZE regime: wait for expansion confirmation")

    return _flat("No matching regime rule")


if __name__ == "__main__":
    row = {
        "adx": 27.49,
        "breakout_strength": 0.0,
        "price_vs_vwap": -0.00299,
        "trend_strength_raw": 0,
        "sqz_on": True,
        "in_pb_zone": True,
        "in_bear_pb_zone": True,
        "bear_align": True,
        "bearish_align": True,
        "opening_bearish": True,
        "close": 37808,
        "ema_fast": 37831.9,
        "ema_slow": 37898.39,
        "volume_spike": 1,
        "volume": 687,
        "macd_rising": True,
        "mom_state": 3,
        "recent_low": 37677,
        "vwap": 37921.56,
        "atr": 65.9,
    }

    regime = classify_regime(row)
    signal = route_signal(row, regime_result=regime)
    print(regime)
    print(signal)
