"""
Bar-level regime classification for futures routing.

This module complements ``core.market_regime``:
- ``core.market_regime`` classifies the broader session/day environment
- this file classifies the current enriched bar for deterministic routing
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class FuturesBarRegimeConfig:
    """Thresholds for bar-level futures regime classification."""

    adx_trend_threshold: float = 22.0
    adx_weak_threshold: float = 15.0
    
    # ATR-normalized thresholds (V-Model Upgrade)
    breakout_strength_trend_threshold: float = 0.25    # Base threshold (ATR units)
    bear_breakout_strength_trend_threshold: float = 0.25
    
    # Regime-aware sensitivity (multipliers)
    trend_regime_threshold_mult: float = 0.60         # 0.25 * 0.6 = 0.15 in TRENDING
    squeeze_regime_threshold_mult: float = 1.0        # Keep 0.25 in SQUEEZE
    
    min_volume_spike_confirmation: float = 1.5        # Stage 3: Confirmation
    stretched_vwap_distance: float = 0.0035
    trend_strength_threshold: float = 0.001
    min_volume_spike: float = 1.0
    min_alignment_score: int = 2


@dataclass(frozen=True)
class FuturesBarRegimeResult:
    """Structured routing hint for one futures bar."""

    regime: str
    bias: str
    confidence: float
    reasons: list[str]
    session_regime: str = "UNKNOWN"


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


def _normalize_session_regime(session_regime: Any) -> str:
    if session_regime is None:
        return "UNKNOWN"
    value = getattr(session_regime, "value", session_regime)
    text = str(value).strip().upper()
    return text or "UNKNOWN"


def _in_pullback_zone(row: Mapping[str, Any]) -> bool:
    return any(
        (
            _as_bool(row.get("in_pb_zone")),
            _as_bool(row.get("in_bear_pb_zone")),
            _as_bool(row.get("in_bull_pb_zone")),
        )
    )


def _infer_bias(
    row: Mapping[str, Any],
    min_alignment_score: int,
) -> tuple[str, list[str]]:
    reasons: list[str] = []

    price_vs_vwap = _safe_float(row.get("price_vs_vwap"))
    close = _safe_float(row.get("close", row.get("Close")))
    ema_fast = _safe_float(row.get("ema_fast", row.get("ema_filter")))
    ema_slow = _safe_float(row.get("ema_slow", row.get("ema_macro")))
    trend_strength_raw = _safe_float(row.get("trend_strength_raw"))

    bull_score = sum(
        [
            _as_bool(row.get("bull_align")),
            _as_bool(row.get("bullish_align")),
            _as_bool(row.get("opening_bullish")),
            price_vs_vwap > 0,
            close > ema_fast if ema_fast else False,
            ema_fast > ema_slow if ema_fast and ema_slow else False,
            trend_strength_raw > 0,
        ]
    )
    bear_score = sum(
        [
            _as_bool(row.get("bear_align")),
            _as_bool(row.get("bearish_align")),
            _as_bool(row.get("opening_bearish")),
            price_vs_vwap < 0,
            close < ema_fast if ema_fast else False,
            ema_fast < ema_slow if ema_fast and ema_slow else False,
            trend_strength_raw < 0,
        ]
    )

    if bull_score >= min_alignment_score and bull_score > bear_score:
        reasons.append(f"bullish evidence={bull_score} > bearish evidence={bear_score}")
        return "LONG", reasons
    if bear_score >= min_alignment_score and bear_score > bull_score:
        reasons.append(f"bearish evidence={bear_score} > bullish evidence={bull_score}")
        return "SHORT", reasons

    reasons.append("directional evidence insufficient or balanced")
    return "NEUTRAL", reasons


def classify_futures_bar_regime(
    row: Mapping[str, Any],
    config: FuturesBarRegimeConfig | None = None,
    *,
    session_regime: Any = None,
) -> FuturesBarRegimeResult:
    """
    Classify one enriched futures bar for routing.

    Regime labels:
    - TREND: strong directional continuation
    - WEAK: choppy / incomplete directional confirmation
    - STRETCHED: extended away from VWAP in a pullback zone
    - SQUEEZE: compression; stand aside until expansion confirms
    """

    cfg = config or FuturesBarRegimeConfig()
    reasons: list[str] = []
    normalized_session_regime = _normalize_session_regime(session_regime)

    adx = _safe_float(row.get("adx"))
    breakout_strength = _safe_float(row.get("breakout_strength"))
    bear_breakout_strength = _safe_float(row.get("bear_breakout_strength"))
    price_vs_vwap = _safe_float(row.get("price_vs_vwap"))
    trend_strength_raw = _safe_float(row.get("trend_strength_raw"))
    sqz_on = _as_bool(row.get("sqz_on"))
    volume_spike = _safe_float(row.get("volume_spike"))

    bias, bias_reasons = _infer_bias(row, cfg.min_alignment_score)
    reasons.extend(bias_reasons)
    if normalized_session_regime != "UNKNOWN":
        reasons.append(f"session regime={normalized_session_regime}")

    # [Fix] RegimeDebug: log feature values for diagnostic
    close = _safe_float(row.get("close") or row.get("Close"))
    day_open = _safe_float(row.get("day_open"))
    vwap = _safe_float(row.get("vwap"))
    ema_fast = _safe_float(row.get("ema_fast"))
    ema_slow = _safe_float(row.get("ema_slow"))
    intraday_return = ((close - day_open) / day_open * 100) if day_open > 0 else 0.0
    import logging
    logging.getLogger("regime").info(
        "[RegimeDebug] close=%.2f day_open=%.2f vwap=%.2f "
        "intraday_return=%.4f%% bull_bs=%.4f bear_bs=%.4f adx=%.2f "
        "trend_strength_raw=%.6f sqz_on=%s volume_spike=%.2f "
        "bias=%s session_regime=%s",
        close, day_open, vwap,
        intraday_return, breakout_strength, bear_breakout_strength, adx,
        trend_strength_raw, sqz_on, volume_spike,
        bias, normalized_session_regime,
    )

    if sqz_on and adx < cfg.adx_trend_threshold:
        reasons.append("squeeze active while ADX is below trend threshold")
        return FuturesBarRegimeResult(
            regime="SQUEEZE",
            bias=bias,
            confidence=0.70,
            reasons=reasons,
            session_regime=normalized_session_regime,
        )

    if abs(price_vs_vwap) >= cfg.stretched_vwap_distance and _in_pullback_zone(row):
        reasons.append(
            f"price stretched from VWAP ({abs(price_vs_vwap):.4f}) inside pullback zone"
        )
        return FuturesBarRegimeResult(
            regime="STRETCHED",
            bias=bias,
            confidence=0.75,
            reasons=reasons,
            session_regime=normalized_session_regime,
        )

    # [V-Model Upgrade] Dynamic Thresholds & Confirmation
    bs_threshold = cfg.breakout_strength_trend_threshold
    if normalized_session_regime == "TRENDING":
        bs_threshold *= cfg.trend_regime_threshold_mult
    elif normalized_session_regime == "SQUEEZE":
        bs_threshold *= cfg.squeeze_regime_threshold_mult

    # ═══ Session Open Buffer (V-Model Safety) ═══
    # 避免夜盤開盤前幾根 Bar 因量能不穩導致誤判
    bars_since_open = _safe_float(row.get("bars_since_open", 999))
    volume_confirmed = volume_spike >= cfg.min_volume_spike_confirmation
    if bars_since_open < 5:
        volume_confirmed = False # Early session: disable volume confirmation for safety
        if volume_spike >= cfg.min_volume_spike_confirmation:
            reasons.append("SESSION_BUFFER_SKIP: volume spike ignored due to early session (<5 bars)")

    # ═══ Three-Stage Breakout Logic (V2) ═══
    # 1. Structure: is_structural_breakout != 0 (Close > High20_prev)
    # 2. Strength:  breakout_strength_atr >= adjusted threshold
    # 3. Confirm:   Volume Spike (buffered) + VWAP Alignment
    
    is_structural = _safe_float(row.get("is_structural_breakout"))
    
    # BULL Breakout
    if bias == "LONG" and is_structural == 1:
        if breakout_strength < bs_threshold:
            reasons.append(f"ATR_GATE_FAIL: bull bs_atr={breakout_strength:.2f} < {bs_threshold:.2f}")
        
    bull_confirmed = (
        bias == "LONG"
        and is_structural == 1
        and breakout_strength >= bs_threshold
        and volume_confirmed
        and (close > vwap if vwap > 0 else True)
    )
    
    atr_trace = f"atr[raw={row.get('atr_raw',0):.1f}, floor={row.get('atr_floor',0):.1f}, used={row.get('atr_used',0):.1f}]"

    if bull_confirmed:
        reasons.append(
            f"BULL breakout confirmed (V2): bs_atr={breakout_strength:.2f} >= {bs_threshold:.2f}, "
            f"vol={volume_spike:.2f} (bars={bars_since_open:.0f}), {atr_trace}"
        )
        confidence = 0.88 if normalized_session_regime == "TRENDING" else 0.85
        return FuturesBarRegimeResult(
            regime="TREND",
            bias=bias,
            confidence=confidence,
            reasons=reasons,
            session_regime=normalized_session_regime,
        )

    # BEAR Breakout
    if bias == "SHORT" and is_structural == -1:
        if bear_breakout_strength < bs_threshold:
            reasons.append(f"ATR_GATE_FAIL: bear bs_atr={bear_breakout_strength:.2f} < {bs_threshold:.2f}")

    bear_confirmed = (
        bias == "SHORT"
        and is_structural == -1
        and bear_breakout_strength >= bs_threshold
        and volume_confirmed
        and (close < vwap if vwap > 0 else True)
    )
    
    if bear_confirmed:
        reasons.append(
            f"BEAR breakout confirmed (V2): bear_bs_atr={bear_breakout_strength:.2f} >= {bs_threshold:.2f}, "
            f"vol={volume_spike:.2f} (bars={bars_since_open:.0f}), {atr_trace}"
        )
        _bear_c = 0.80 if adx >= cfg.adx_trend_threshold else 0.65
        return FuturesBarRegimeResult(
            regime="BEAR",
            bias=bias,
            confidence=_bear_c,
            reasons=reasons,
            session_regime=normalized_session_regime,
        )

    moderate_directional_pressure = (
        bias != "NEUTRAL"
        and adx >= cfg.adx_weak_threshold
        and abs(trend_strength_raw) >= cfg.trend_strength_threshold
    )
    # ═══ Momentum Override: prevent WEAK when momentum is strong ═══
    # WEAK + mom_state >= 3 + price above VWAP is a contradiction.
    # If momentum is high and bias is directional, upgrade to TREND/TRANSITION.
    _mom_state = int(_safe_float(row.get("mom_state", 0)))
    _mom_velo = _safe_float(row.get("mom_velo", 0))
    _mom_val = _safe_float(row.get("momentum", 0))
    if (moderate_directional_pressure
        and (_mom_state >= 3 or (_mom_val > 500 and _mom_velo > 100))
        and bias in ("LONG", "SHORT")
        and (close > vwap if vwap > 0 else True)
    ):
        reasons.append(
            f"MOMENTUM_OVERRIDE: mom_state={_mom_state} mom={_mom_val:.0f} "
            f"mom_velo={_mom_velo:.0f} bias={bias} — upgrading from WEAK to TRANSITION"
        )
        confidence = 0.75 if normalized_session_regime == "TRENDING" else 0.65
        return FuturesBarRegimeResult(
            regime="TRANSITION",
            bias=bias,
            confidence=confidence,
            reasons=reasons,
            session_regime=normalized_session_regime,
        )

    if moderate_directional_pressure:
        reasons.append("directional pressure exists, but breakout confirmation is incomplete")
        confidence = 0.60 if normalized_session_regime == "TRENDING" else 0.55
        return FuturesBarRegimeResult(
            regime="WEAK",
            bias=bias,
            confidence=confidence,
            reasons=reasons,
            session_regime=normalized_session_regime,
        )

    if normalized_session_regime == "SHOCK":
        reasons.append("session regime is shock; keep bar classification conservative")

    reasons.append("defaulted to weak/choppy bar regime")
    return FuturesBarRegimeResult(
        regime="WEAK",
        bias=bias,
        confidence=0.50,
        reasons=reasons,
        session_regime=normalized_session_regime,
    )


def describe_futures_bar_regime(result: FuturesBarRegimeResult) -> str:
    """Single-line description for audit logs."""

    why = "; ".join(result.reasons)
    return (
        f"regime={result.regime} bias={result.bias} "
        f"confidence={result.confidence:.2f} session={result.session_regime} "
        f"reasons=[{why}]"
    )
