"""
mts_micro_vwap.py — Pure Micro-VWAP indicator (spec section 3) for MTS 2.0.

Implements the rolling micro VWAP per docs/mts/mts_trend_signals_formal_spec.md:
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
DEFAULT_WINDOW_SECS = 900.0           # 15m rolling window (spec 3.1)
DEFAULT_MAX_SAMPLE_AGE_SECS = 60.0    # fail-closed if the newest sample is older than this


def _to_epoch_secs(value: Any) -> Optional[float]:
    """Normalize a ts value to epoch seconds (float). Returns None if unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # already epoch (s or ms): normalize ms->s heuristically
        f = float(value)
        if f > 1e12:   # epoch ms
            return f / 1000.0
        return f
    import pandas as pd
    try:
        return pd.Timestamp(value).timestamp()
    except Exception:
        return None


@dataclass(frozen=True)
class MicroVWAPResult:
    decision_ts: str
    vwap: float
    vwap_std: float
    last_price: float
    deviation: float                 # price - vwap (points)
    atr_1m: float                    # ATR used for the deviation gate
    deviation_status: VwapDeviation
    n_samples: int                   # 5-sec samples in the finished (windowed) set
    n_samples_in_window: int         # samples kept after window filter
    samples_missing: bool = False    # True if the window was empty / stale / zero-vol

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_micro_vwap(decision_ts: Any,
                       samples: Sequence[dict[str, Any]],
                       atr_1m: float,
                       *,
                       window_secs: float = DEFAULT_WINDOW_SECS,
                       max_sample_age_secs: float = DEFAULT_MAX_SAMPLE_AGE_SECS,
                       ) -> MicroVWAPResult:
    """Compute the micro VWAP over FINISHED 5-sec samples, window-filtered.

    samples: time-ordered list of {ts, price, vol} 5-sec resamples (all should be
             <= decision_ts). ts may be epoch-s/ms or datetime/ISO.
    decision_ts: the as-of boundary. Only samples in
             [decision_ts - window_secs, decision_ts] are used (spec 5.3 #3:
             "Micro-VWAP 的 5 秒採樣必須由原始 tick 重採樣；無 tick 的窗口不得
             前值填充為 fresh quote").
    atr_1m: 1-min ATR used for the deviation threshold.

    Fail-closed:
      - decision_ts or a sample ts unparseable -> UNKNOWN
      - zero samples in window -> UNKNOWN (samples_missing=True)
      - the newest kept sample is STALE (> max_sample_age_secs before
        decision_ts) -> UNKNOWN (gap / no fresh quote; never carry forward)
      - zero volume or non-positive ATR -> UNKNOWN
    """
    dts = _to_epoch_secs(decision_ts)
    if dts is None:
        return MicroVWAPResult(
            decision_ts=decision_ts, vwap=0.0, vwap_std=0.0, last_price=0.0,
            atr_1m=atr_1m, deviation=0.0,
            deviation_status=VwapDeviation.UNKNOWN, n_samples=0, n_samples_in_window=0,
            samples_missing=True,
        )

    win_min = dts - window_secs
    px, vol = [], []
    for s in samples:
        sts = _to_epoch_secs(s.get("ts"))
        if sts is None or not (win_min <= sts <= dts):
            continue
        p = s.get("price"); v = s.get("volume", 0)
        if p is not None and v > 0:
            px.append(p); vol.append(v)

    kept = len(px)
    if not px or not vol:
        return MicroVWAPResult(
            decision_ts=decision_ts, vwap=0.0, vwap_std=0.0, last_price=0.0,
            atr_1m=atr_1m, deviation=0.0,
            deviation_status=VwapDeviation.UNKNOWN, n_samples=0, n_samples_in_window=kept,
            samples_missing=True,
        )
    # staleness: newest kept sample must be fresh
    last_ts = None
    for s in samples:
        ts2 = _to_epoch_secs(s.get("ts"))
        if ts2 is not None and win_min <= ts2 <= dts:
            last_ts = ts2
    if last_ts is None or (dts - last_ts) > max_sample_age_secs:
        return MicroVWAPResult(
            decision_ts=decision_ts, vwap=0.0, vwap_std=0.0, last_price=0.0,
            atr_1m=atr_1m, deviation=0.0,
            deviation_status=VwapDeviation.UNKNOWN, n_samples=len(px), n_samples_in_window=kept,
            samples_missing=True,
        )

    total_vol = sum(vol)
    if total_vol <= 0 or atr_1m <= 0:
        return MicroVWAPResult(
            decision_ts=decision_ts, vwap=0.0, vwap_std=0.0, last_price=0.0,
            atr_1m=atr_1m, deviation=0.0,
            deviation_status=VwapDeviation.UNKNOWN, n_samples=len(px), n_samples_in_window=kept,
            samples_missing=True,
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
        n_samples_in_window=kept, samples_missing=False,
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