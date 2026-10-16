"""
mts_trend_signal_adapter.py — Pure Trend-Confirmation Signal Adapter for MTS 2.0.

Implements the formal spec (docs/mts/mts_trend_signals_formal_spec.md):
  ADL SNR -> Renko bricks -> Micro-VWAP  ->  2-of-3 arbitration -> Confidence.

This module is PURE (no broker, no wall-clock as decision source, no side effects).
All signal state is computed from an immutable decision snapshot built at decision_ts.

Design principles (spec section 5):
  * decision_ts immutable snapshot — all sub-signals read the SAME asof boundary.
  * completed bars only — ADL uses ONLY completed 5m bars at/before decision_ts.
  * MID/last pricing for direction signals (NEVER executable bid/ask).
  * 2-of-3 same direction, no negative sub-signal, Confidence >= 0.70 -> PASS.
  * any divergence / data staleness / warmup gap -> BLOCK (fail-closed).

Sub-signal semantics (spec 4.2):
  Renko:  S=1.0 if >=2 same-direction bricks; 0.0 if <2; -1.0 if reverse brick.
  ADL:    S=1.0 if |SNR|>1.8 and same direction; 0.5 if |SNR|<=1.8 (CHOP);
with 0.5;
          -1.0 if opposite direction.
  VWAP:   S=1.0 if deviation same-direction; 0.5 if NEUTRAL; -1.0 if reverse.

  Confidence = 0.45*S_Ren + 0.35*S_ADL + 0.20*S_VWAP
  PASS iff Confidence >= 0.70 AND no sub-signal negative AND >=2 signals agree.

PRICING RULE: all three signal inputs use the SAME non-direction-biased price
(MID when available, else last trade). Executable bid/ask NEVER feeds a
direction signal (spec 5.2) — reserved for fill/slippage simulation only.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional, Sequence


class TrendDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    CHOP = "CHOP"
    UNKNOWN = "UNKNOWN"


class VwapDeviation(str, Enum):
    ABOVE = "ABOVE"
    BELOW = "BELOW"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class RenkoState(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"        # <2 bricks yet (insufficient)
    UNKNOWN = "UNKNOWN"


# Confidence weights (spec 4.2) — canonical
W_RENKO = 0.45
W_ADL = 0.35
W_VWAP = 0.20
CONFIDENCE_THRESHOLD = 0.70
ADL_SNR_THRESHOLD = 1.8

# sub-signal scores
S_RENKO_SAME = 1.0
S_RENKO_NONE = 0.0
S_RENKO_OPPOSITE = -1.0
S_ADL_SAME = 1.0
S_ADL_CHOP = 0.5
S_ADL_OPPOSITE = -1.0
S_VWAP_SAME = 1.0
S_VWAP_NEUTRAL = 0.5
S_VWAP_OPPOSITE = -1.0


@dataclass(frozen=True)
class SubSignalState:
    source: str
    direction: TrendDirection
    score: float
    asof_ts: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrendDecision:
    decision_ts: str
    direction: TrendDirection
    confidence: float
    renko: SubSignalState
    adl: SubSignalState
    vwap: SubSignalState
    pass_release: bool
    block_reason: Optional[str] = None
    decision_max_quote_age_ms: Optional[float] = None
    window_max_quote_age_ms: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _same_direction(a: TrendDirection, b: TrendDirection) -> bool:
    return a in (TrendDirection.BULLISH, TrendDirection.BEARISH) and a == b


# ── ADL SNR (spec section 1) — pure, uses only completed 5m OHLCV bars ──

_ADL_WINDOW_N = 12          # N bars rolling window
_SNR_EPS = 1e-6


@dataclass(frozen=True)
class AdlSnrResult:
    """ADL-SNR per a decision_ts (spec 1.1): slope, residual std, snr, direction."""
    decision_ts: str
    adl_value: float
    slope: float
    residual_std: float
    snr: float
    direction: TrendDirection       # BULLISH if snr>+1.8 & slope>0, etc.
    n_bars: int                     # completed bars in window (warmup gate)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_adl_snr(decision_ts: Any,
                    bars: Sequence[dict[str, Any]],
                    window_n: int = _ADL_WINDOW_N,
                    ) -> AdlSnrResult:
    """Compute ADL SNR from a strictly-ordered list of COMPLETED 5m OHLCV bars.

    bars must be the LAST `window_n` completed bars ending at/before decision_ts
    (oldest first). Each bar needs keys: high, low, close, volume.

    Spec 1.1:
      MFM = ((C-L) - (H-C)) / (H-L),  if H==L -> 0
      MFV = MFM * V
      ADL_t = ADL_{t-1} + MFV_t
      OLS slope beta + residual std over N bars
      SNP = (beta * N) / (residual_std + eps)

    Fail-closed (spec 5.3): fewer than window_n bars, zero/negative volume, or
    residual variance not computable -> direction UNKNOWN (BLOCK).
    """
    if len(bars) < window_n:
        return AdlSnrResult(
            decision_ts=decision_ts, adl_value=0.0, slope=0.0,
            residual_std=0.0, snr=0.0,
            direction=TrendDirection.UNKNOWN,
            n_bars=len(bars),
        )

    # build ADL series
    adl_series: list[float] = []
    cur: float = 0.0
    for b in bars:
        h = b.get("high"); l = b.get("low"); c = b.get("close"); v = b.get("volume")
        if h is None or l is None or c is None or v is None or v <= 0:
            return AdlSnrResult(
                decision_ts=decision_ts, adl_value=0.0, slope=0.0,
                residual_std=0.0, snr=0.0,
                direction=TrendDirection.UNKNOWN,
                n_bars=len(bars),
            )
        denom = float(h) - float(l)
        mfm = 0.0 if denom == 0 else ((float(c) - float(l)) - (float(h) - float(c))) / denom
        cur += mfm * float(v)
        adl_series.append(cur)

    # OLS: y = alpha + beta*i over i=0..N-1
    n = len(adl_series)
    xs = list(range(n))
    xm = sum(xs) / n
    ym = sum(adl_series) / n
    sxy = sum((xs[i] - xm) * (adl_series[i] - ym) for i in range(n))
    sxx = sum((xs[i] - xm) ** 2 for i in range(n))
    if sxx == 0:
        return AdlSnrResult(
            decision_ts=decision_ts, adl_value=cur, slope=0.0,
            residual_std=0.0, snr=0.0, direction=TrendDirection.UNKNOWN,
            n_bars=n,
        )
    beta = sxy / sxx
    alpha = ym - beta * xm
    resid = [adl_series[i] - (alpha + beta * xs[i]) for i in range(n)]
    resid_var = sum(r * r for r in resid) / (n - 2) if n > 2 else 0.0
    residual_std = resid_var ** 0.5

    snr = (beta * n) / (residual_std + _SNR_EPS) if residual_std > 0 else 0.0

    if snr > ADL_SNR_THRESHOLD and beta > 0:
        direction = TrendDirection.BULLISH
    elif snr < -ADL_SNR_THRESHOLD and beta < 0:
        direction = TrendDirection.BEARISH
    else:
        direction = TrendDirection.CHOP

    return AdlSnrResult(
        decision_ts=decision_ts, adl_value=cur, slope=beta,
        residual_std=residual_std, snr=snr, direction=direction, n_bars=n,
    )


def adl_signal_state(adl: AdlSnrResult, expected: TrendDirection) -> SubSignalState:
    """Map an AdlSnrResult to a SubSignalState scored against the retained-leg direction.

    S_ADL = 1.0 if |SNR|>1.8 and same direction; 0.5 if CHOP; -1.0 if opposite.
    UNKNOWN (insufficient data) -> score -1.0 + UNKNOWN direction (fail-closed).
    """
    if adl.direction == TrendDirection.UNKNOWN:
        return SubSignalState(source="adl", direction=TrendDirection.UNKNOWN,
                               score=S_ADL_OPPOSITE, detail=adl.to_dict())
    if adl.direction == TrendDirection.CHOP:
        return SubSignalState(source="adl", direction=TrendDirection.CHOP,
                               score=S_ADL_CHOP, detail=adl.to_dict())
    if adl.direction == expected:
        return SubSignalState(source="adl", direction=adl.direction,
                               score=S_ADL_SAME, detail=adl.to_dict())
    return SubSignalState(source="adl", direction=adl.direction,
                           score=S_ADL_OPPOSITE, detail=adl.to_dict())


def arbitrate_trend(
    decision_ts: Any,
    renko: SubSignalState,
    adl: SubSignalState,
    vwap: SubSignalState,
    *,
    decision_max_quote_age_ms: Optional[float] = None,
    window_max_quote_age_ms: Optional[float] = None,
) -> TrendDecision:
    """Arbitrate the three sub-signals into one TrendDecision (spec 4.1/5.4)."""
    signals = [renko, adl, vwap]
    conf = round(W_RENKO * renko.score + W_ADL * adl.score + W_VWAP * vwap.score, 4)

    # 1) any UNKNOWN direction or any negative(-1.0) sub-signal => DIVERGENCE/BLOCK
    for s in signals:
        if s.direction == TrendDirection.UNKNOWN or s.score == -1.0:
            return TrendDecision(
                decision_ts=decision_ts,
                direction=TrendDirection.CHOP,
                confidence=conf,
                renko=renko, adl=adl, vwap=vwap,
                pass_release=False,
                block_reason="DIVERGENCE_OR_INSUFFICIENT",
                decision_max_quote_age_ms=decision_max_quote_age_ms,
                window_max_quote_age_ms=window_max_quote_age_ms,
            )

    dirs = [s.direction for s in signals if s.direction in (TrendDirection.BULLISH, TrendDirection.BEARISH)]
    # 2) need >=2 same-direction
    if len(dirs) < 2 or not all(d == dirs[0] for d in dirs):
        return TrendDecision(
            decision_ts=decision_ts,
            direction=dirs[0] if len(dirs) == 1 else TrendDirection.CHOP,
            confidence=conf,
            renko=renko, adl=adl, vwap=vwap,
            pass_release=False,
            block_reason="INSUFFICIENT_SAME_DIRECTION" if len(dirs) < 2 else None,
            decision_max_quote_age_ms=decision_max_quote_age_ms,
            window_max_quote_age_ms=window_max_quote_age_ms,
        )

    direction = dirs[0]
    # 3) PASS iff confidence >= 0.70 (direction handled above)
    return TrendDecision(
        decision_ts=decision_ts,
        direction=direction,
        confidence=conf,
        renko=renko, adl=adl, vwap=vwap,
        pass_release=(conf >= CONFIDENCE_THRESHOLD),
        block_reason=None if conf >= CONFIDENCE_THRESHOLD else "CONFIDENCE_BELOW_THRESHOLD",
        decision_max_quote_age_ms=decision_max_quote_age_ms,
        window_max_quote_age_ms=window_max_quote_age_ms,
    )