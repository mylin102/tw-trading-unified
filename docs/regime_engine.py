"""
regime_engine.py

Regime classification utilities for futures auto-trading.
Designed for the user's feature-rich kbar schema.

Core idea:
- Separate market condition detection from signal generation.
- Keep thresholds configurable and explicit.
- Return structured outputs so strategy_router can remain deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass(frozen=True)
class RegimeConfig:
    """Thresholds used by regime classification."""
    adx_trend_threshold: float = 30.0
    adx_weak_threshold: float = 20.0
    breakout_strength_trend_threshold: float = 0.60
    vwap_distance_stretched: float = 0.0035
    trend_strength_threshold: float = 1.0
    min_alignment_score: int = 1


@dataclass(frozen=True)
class RegimeResult:
    """Normalized regime classification output."""
    regime: str
    bias: str
    confidence: float
    reasons: list[str]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _infer_bias(row: Dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []

    bull_flags = sum(
        [
            _as_bool(row.get("bull_align")),
            _as_bool(row.get("bullish_align")),
            _as_bool(row.get("opening_bullish")),
            _safe_float(row.get("price_vs_vwap")) > 0,
            _safe_float(row.get("close")) > _safe_float(row.get("ema_fast")),
            _safe_float(row.get("ema_fast")) > _safe_float(row.get("ema_slow")),
        ]
    )

    bear_flags = sum(
        [
            _as_bool(row.get("bear_align")),
            _as_bool(row.get("bearish_align")),
            _as_bool(row.get("opening_bearish")),
            _safe_float(row.get("price_vs_vwap")) < 0,
            _safe_float(row.get("close")) < _safe_float(row.get("ema_fast")),
            _safe_float(row.get("ema_fast")) < _safe_float(row.get("ema_slow")),
        ]
    )

    if bull_flags > bear_flags:
        reasons.append(f"bullish evidence={bull_flags} > bearish evidence={bear_flags}")
        return "LONG", reasons
    if bear_flags > bull_flags:
        reasons.append(f"bearish evidence={bear_flags} > bullish evidence={bull_flags}")
        return "SHORT", reasons

    reasons.append("directional evidence balanced")
    return "NEUTRAL", reasons


def classify_regime(
    row: Dict[str, Any],
    config: Optional[RegimeConfig] = None,
) -> RegimeResult:
    """
    Classify one bar into a regime for downstream strategy routing.

    Regime labels:
    - TREND: directional continuation environment
    - WEAK: choppy / low-conviction / fake-break prone
    - STRETCHED: overextended away from VWAP/BBands, prefer mean reversion
    - SQUEEZE: compression, stand by for expansion
    """
    cfg = config or RegimeConfig()
    reasons: list[str] = []

    adx = _safe_float(row.get("adx"))
    breakout_strength = _safe_float(row.get("breakout_strength"))
    price_vs_vwap = abs(_safe_float(row.get("price_vs_vwap")))
    trend_strength_raw = _safe_float(row.get("trend_strength_raw"))
    sqz_on = _as_bool(row.get("sqz_on"))
    in_pb_zone = _as_bool(row.get("in_pb_zone"))
    volume_spike = _safe_float(row.get("volume_spike"))
    volume = _safe_float(row.get("volume"))

    bias, bias_reasons = _infer_bias(row)
    reasons.extend(bias_reasons)

    # Highest-priority classification: squeeze
    if sqz_on and adx < cfg.adx_trend_threshold:
        reasons.append("squeeze active while ADX not yet confirming trend")
        return RegimeResult(
            regime="SQUEEZE",
            bias=bias,
            confidence=0.70,
            reasons=reasons,
        )

    # Overstretched away from VWAP / bands
    if price_vs_vwap >= cfg.vwap_distance_stretched and in_pb_zone:
        reasons.append(
            f"price stretched from VWAP ({price_vs_vwap:.4f}) in pullback zone"
        )
        return RegimeResult(
            regime="STRETCHED",
            bias=bias,
            confidence=0.75,
            reasons=reasons,
        )

    trend_confirmed = (
        adx >= cfg.adx_trend_threshold
        and breakout_strength >= cfg.breakout_strength_trend_threshold
    )
    if trend_confirmed:
        reasons.append(
            f"ADX={adx:.2f} and breakout_strength={breakout_strength:.2f} confirm trend"
        )
        return RegimeResult(
            regime="TREND",
            bias=bias,
            confidence=0.85,
            reasons=reasons,
        )

    moderate_trend = (
        adx >= cfg.adx_weak_threshold
        and abs(trend_strength_raw) >= cfg.trend_strength_threshold
        and volume > 0
    )
    if moderate_trend and volume_spike >= 1:
        reasons.append(
            "moderate trend evidence exists, but breakout confirmation is incomplete"
        )
        return RegimeResult(
            regime="WEAK",
            bias=bias,
            confidence=0.55,
            reasons=reasons,
        )

    reasons.append("defaulted to weak/choppy regime")
    return RegimeResult(
        regime="WEAK",
        bias=bias,
        confidence=0.50,
        reasons=reasons,
    )


def describe_regime(result: RegimeResult) -> str:
    """Human-readable single-line description for logs."""
    why = "; ".join(result.reasons)
    return (
        f"regime={result.regime} bias={result.bias} "
        f"confidence={result.confidence:.2f} reasons=[{why}]"
    )


if __name__ == "__main__":
    sample_row = {
        "adx": 27.49,
        "breakout_strength": 0.0,
        "price_vs_vwap": -0.00299,
        "trend_strength_raw": 0,
        "sqz_on": True,
        "in_pb_zone": True,
        "bear_align": True,
        "bearish_align": True,
        "opening_bearish": True,
        "close": 37808,
        "ema_fast": 37831.9,
        "ema_slow": 37898.39,
        "volume_spike": 1,
        "volume": 687,
    }
    result = classify_regime(sample_row)
    print(describe_regime(result))
