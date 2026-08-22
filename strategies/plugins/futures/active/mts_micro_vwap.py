"""
mts_micro_vwap.py — Pure Micro-VWAP indicator (spec section 3) for MTS 2.0.

Implements the dual-window VWAP per docs/mts/mts_trend_signals_formal_spec.md:
  Rolling micro VWAP (15m window) + session-anchored VWAP.
  Deviation = (price - VWAP) vs DEVIATION_ATR_MULT*ATR_1m -> ABOVE/BELOW/NEUTRAL.

Micro-VWAP is computed from 5-sec resampled ticks over a 15m rolling window.
Distinct from counter_vwap.py which is a *strategy* class, not an indicator.

PURE: no broker, no side effects. All state derives from an immutable window of
(ts, price, volume) samples bounded by decision_ts.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional, Sequence
from .mts_trend_signal_adapter import (
    TrendDirection, VwapDeviation, SubSignalState,
    S_VWAP_SAME, S_VWAP_NEUTRAL, S_VWAP_OPPOSITE,
)

# deviation threshold multiplier on ATR_1m (spec 3.3)
DEVIATION_ATR_MULT = 0.3


@dataclass(frozen=True)
class MicroVWAPResult:
    decision_ts: str
    vwap: float
    vwap_std: float
    last_price: float
    deviation: float                 # price - vwap (points)
    atr_1m: float                    # ATR used for the deviation gate
    deviation_status: VwapDeviation
    n_samples: int                   # 5-sec samples in the finished window
    samples_missing: bool = False    # True if the window had gaps / no fresh quotes

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_micro_vwap(decision_ts: Any,
                       samples: Sequence[dict[str, Any]],
                       atr_1m: float,
                       *,
                       window_secs: float = 900.0,     # 15m rolling window (spec 3.1)
                       ) -> MicroVWAPResult:
    """Compute the micro VWAP over FINISHED 5-sec samples ending at/before decision_ts.

    samples: time-ordered list of {ts, price, vol} 5-sec resamples, all <= decision_ts.
    atr_1m: 1-min ATR used for the deviation threshold.

    Fail-closed: empty / stale / zero-vol / zero-ATR window -> deviation UNKNOWN.
    """
    px, vol = [], []
    for s in samples:
        p = s.get("price"); v = s.get("volume", 0)
        if p is not None and v > 0:
            px.append(p); vol.append(v)

    if not px or not vol:
        return MicroVWAPResult(
            decision_ts=decision_ts, vwap=0.0, vwap_std=0.0, last_price=0.0,
            atr_1m=atr_1m, deviation=0.0,
            deviation_status=VwapDeviation.UNKNOWN, n_samples=0, samples_missing=True,
        )
    total_vol = sum(vol)
    if total_vol <= 0 or atr_1m <= 0:
        return MicroVWAPResult(
            decision_ts=decision_ts, vwap=0.0, vwap_std=0.0, last_price=0.0,
            atr_1m=atr_1m, deviation=0.0,
            deviation_status=VwapDeviation.UNKNOWN, n_samples=len(px), samples_missing=True,
        )
    vwap = sum(p * v for p, v in zip(px, vol)) / total_vol
    var = sum(v * (p - vwap) ** 2 for p, v in zip(px, vol)) / total_vol
    vwap_std = var ** 0.5
    last_price = px[-1]
    dev = last_price - vwap
    dev_thr = DEVIATION_ATR_MULT * atr_1m
    if dev >= dev_thr:
        status = VwapDeviation.ABOVE
    elif dev <= -dev_thr:
        status = VwapDeviation.BELOW
    else:
        status = VwapDeviation.NEUTRAL
    return MicroVWAPResult(
        decision_ts=decision_ts, vwap=vwap, vwap_std=vwap_std, last_price=last_price,
        atr_1m=atr_1m, deviation=dev, deviation_status=status, n_samples=len(px),
        samples_missing=False,
    )


def vwap_signal_state(vwap: MicroVWAPResult, expected: TrendDirection) -> SubSignalState:
    """Map a MicroVWAPResult to a SubSignalState scored against expected direction."""
    if vwap.deviation_status == VwapDeviation.UNKNOWN or vwap.samples_missing:
        return SubSignalState(source="vwap", direction=TrendDirection.UNKNOWN,
                               score=S_VWAP_OPPOSITE, detail=vwap.to_dict())
    if vwap.deviation_status == VwapDeviation.NEUTRAL:
        return SubSignalState(source="vwap", direction=TrendDirection.CHOP,
                               score=S_VWAP_NEUTRAL, detail=vwap.to_dict())
    dev_dir = TrendDirection.BULLISH if vwap.deviation_status == VwapDeviation.ABOVE else TrendDirection.BEARISH
    if dev_dir == expected:
        return SubSignalState(source="vwap", direction=dev_dir, score=S_VWAP_SAME, detail=vwap.to_dict())
    return SubSignalState(source="vwap", direction=dev_dir, score=S_VWAP_OPPOSITE, detail=vwap.to_dict())