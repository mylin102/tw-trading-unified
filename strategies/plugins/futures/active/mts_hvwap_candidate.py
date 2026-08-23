"""
mts_hvwap_candidate.py — Hierarchical VWAP paper candidate arm (MTS 2.0).

2026-08-23 Hermes Agent: parallel, TELEMETRY-ONLY candidate arm implementing
docs/mts/mts_vwap_filter_spec.md (VWAP Directional Filter, Slope &
Multi-Timeframe Hierarchy).

HARD SAFETY CONTRACT
--------------------
* This module NEVER gates the baseline Renko + ADL + Micro-VWAP 2-of-3
  decision arm (mts_trend_signal_adapter.TrendDecision -> TREND_RELEASE).
  It is a pure observer; nothing here can emit an order, block a stop-loss /
  emergency exit / Policy J / timeout / lifecycle reconciliation, or mutate
  strategy state.
* FAIL-CLOSED for the candidate only: unknown / stale / missing-volume /
  incomplete / session-mismatch data yields UNKNOWN/BLOCK, never a pass.
* VWAP semantics (spec section 1.4): per TMF leg, each leg's OWN volume;
  the trading day resets at 15:00 and carries through the next-day session
  (night 15:00 -> next day session); NO prior-trading-day data is used.
* All functions are PURE: no broker, no wall-clock as a decision source, no
  side effects. Bar timestamps are CLOSE times (the moment a bar completes).

Tier hierarchy (spec section 1.2):
  Tier 1: 60m regime verdict       (Trending Bull / Trending Bear / Ranging)
  Tier 2: 15m direction verdict    (continuation / reversal / neutral)
  Tier 3: per-leg session VWAP + slope + ATR-normalized distance
  Tier 4: >= 2 completed 5m bars same-direction confirmation

Candidate statuses (spec section 3):
  UNKNOWN       data unavailable / stale / incomplete (fail-closed)
  BLOCK         a gate actively rejected the candidate
  HOLD          overextended vs VWAP (> 2.5 * ATR_15m) - avoid chasing
  ALIGNED_PASS  every gate passed (telemetry only - NEVER an order)
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional, Sequence

from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    counter_trend_leg_from_sides,
)
from strategies.plugins.futures.active.mts_trend_signal_adapter import (
    TrendDirection,
)

# ── Session / bar constants ───────────────────────────────────────────────
RESET_HOUR = 15                      # VWAP trading day resets at 15:00
BAR_5M_SECS = 300.0
BAR_15M_SECS = 900.0
BAR_60M_SECS = 3600.0
DEFAULT_MAX_QUOTE_AGE_MS = 10000.0   # 10 s (matches tmf_spread _max_quote_age_ms)
DEFAULT_OVEREXTEND_ATR_MULT = 2.5    # spec Q5: > 2.5 * ATR_15m -> HOLD
DEFAULT_DEADBAND_ATR_FRAC = 0.05     # spec section 2: epsilon = 0.05 * ATR_15m
DEFAULT_SLOPE_DELTA_SECS = 600.0     # slope over 2 * 5m bars (spec section 2)
DEFAULT_REGIME_ROC_THRESHOLD = 0.001 # 60m regime ROC threshold (10 bps / 2h)
DEFAULT_15M_FLAT_ROC = 0.0002        # 15m "flat" deadband (2 bps)
DEFAULT_TICK_SIZE = 1.0              # TMF tick


class HvwapStatus(str, Enum):
    ALIGNED_PASS = "ALIGNED_PASS"
    BLOCK = "BLOCK"
    HOLD = "HOLD"
    UNKNOWN = "UNKNOWN"


class Regime60m(str, Enum):
    BULLISH_TREND = "BULLISH_TREND"
    BEARISH_TREND = "BEARISH_TREND"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


class Signal15m(str, Enum):
    CONFIRMED_CONTINUATION = "CONFIRMED_CONTINUATION"
    REVERSAL = "REVERSAL"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class LegVwapSource(str, Enum):
    SESSION_ACCUMULATED = "SESSION_ACCUMULATED"  # pure session VWAP from own-volume samples
    PROVIDED = "PROVIDED"                        # precomputed session VWAP supplied by caller
    MISSING = "MISSING"                          # no volume samples / no provided value


# ── Timestamp helpers ─────────────────────────────────────────────────────

_TW = _dt.timezone(_dt.timedelta(hours=8), name="Asia/Taipei")


def to_epoch_secs(value: Any) -> Optional[float]:
    """Normalize a ts value to epoch seconds (float). None if unparseable.

    Contract (matches tmf_spread.iso_to_epoch_ms legacy semantics):
      * numeric epoch (s or ms, ms auto-detected) -> epoch s
      * pandas Timestamp -> .timestamp() (epoch-accurate; never stringified)
      * tz-aware datetime/ISO -> its UTC epoch
      * naive datetime/ISO -> Asia/Taipei wall clock (NEVER machine-local)
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        f = float(value)
        if f > 1e12:  # epoch ms
            return f / 1000.0
        return f
    import pandas as pd
    if isinstance(value, pd.Timestamp):
        return value.timestamp()
    if isinstance(value, _dt.datetime):
        dt = value
    else:
        try:
            s = str(value).strip()
            if not s:
                return None
            dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            try:
                return pd.Timestamp(value).timestamp()
            except Exception:
                return None
    if dt.tzinfo is None:
        # legacy contract: naive ISO = Asia/Taipei wall-clock
        dt = dt.replace(tzinfo=_TW)
    return dt.timestamp()


def vwap_session_bounds(ts_epoch: float, reset_hour: int = RESET_HOUR) -> tuple[str, float]:
    """Return (session_label, session_start_epoch) for a decision timestamp.

    VWAP trading day: [15:00 day D, 15:00 day D+1). Label = date D.  A time
    before 15:00 belongs to the session that started at 15:00 the PREVIOUS
    calendar day (night session carries through the next day session).
    Raises ValueError when ts_epoch cannot be interpreted.
    """
    if ts_epoch is None or not math.isfinite(float(ts_epoch)):
        raise ValueError("BAD_DECISION_TS")
    dt = _dt.datetime.fromtimestamp(float(ts_epoch), tz=_TW)
    if dt.hour >= reset_hour:
        start_dt = dt.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
    else:
        prev = dt.date() - _dt.timedelta(days=1)
        start_dt = _dt.datetime(prev.year, prev.month, prev.day,
                                reset_hour, 0, 0, tzinfo=_TW)
    return start_dt.date().isoformat(), start_dt.timestamp()


def bar_close_time(ts_value: Any, bar_secs: float = BAR_5M_SECS) -> Optional[float]:
    """Convert a bar timestamp to the bar's CLOSE time (epoch secs), with a
    verified no-double-shift rule.

    Candidate functions require CLOSE-time timestamps. In the production MTS
    pipeline `bar["ts"]` is proven bucket-START (floor(epoch/300)*300 —
    monitor.py tick paths and the tick-bar df index), so the close time is
    ts + bar_secs. The shift is applied ONLY when the timestamp is exactly
    bucket-aligned (epoch % bar_secs == 0); a non-aligned timestamp is
    already a point/close time and is returned unchanged. Returns None for
    unparseable input (fail-closed).
    """
    epoch = to_epoch_secs(ts_value)
    if epoch is None:
        return None
    if int(round(epoch)) % int(bar_secs) == 0:
        return epoch + float(bar_secs)
    return epoch


# ── Session VWAP (spec section 1.4 / 2) ───────────────────────────────────

@dataclass(frozen=True)
class SessionVwapPoint:
    """Cumulative session VWAP at one completed-bar close time."""
    ts: float            # bar close time (epoch secs)
    vwap: float          # cumulative sum(P*V)/sum(V) over the session so far
    cum_volume: float


@dataclass(frozen=True)
class SessionVwapSeries:
    session_label: str
    points: list[SessionVwapPoint] = field(default_factory=list)
    issue: Optional[str] = None      # None | "NO_SAMPLES" | "ZERO_VOLUME" | "STALE" | "INCOMPLETE"
    n_excluded_prior_day: int = 0    # bars dropped by the no-prior-trading-day rule


def compute_session_vwap_series(
    bars: Sequence[dict[str, Any]],
    decision_ts: Any,
    *,
    max_bar_gap_secs: float = 2.0 * BAR_5M_SECS,
) -> SessionVwapSeries:
    """Cumulative session VWAP from per-leg completed bars {ts, close, volume}.

    Contract (spec 1.4):
      * bars must carry CLOSE-time timestamps (when the bar completed).
      * only bars in [session_start(decision_ts), decision_ts] are used —
        anything from a PRIOR trading day (close < session_start) is excluded
        and counted in n_excluded_prior_day (never merged into VWAP).
      * a bar with close > decision_ts is INCOMPLETE -> fail closed.
      * volume must be present and > 0; missing/zero volume -> issue.
      * freshness: the newest completed bar must be within max_bar_gap_secs
        of decision_ts (the in-progress bucket keeps the newest completed
        bar ~300s behind; a gap > 2 bars means MISSING completed bars and
        fails closed — never carried forward).

    Returns a SessionVwapSeries; on any fail-closed condition the series
    carries the issue and an empty (or partial) points list.
    """
    dts = to_epoch_secs(decision_ts)
    if dts is None:
        return SessionVwapSeries(session_label="", issue="BAD_DECISION_TS")
    try:
        label, start = vwap_session_bounds(dts)
    except ValueError:
        return SessionVwapSeries(session_label="", issue="BAD_DECISION_TS")

    excluded = 0
    cum_pv = 0.0
    cum_vol = 0.0
    points: list[SessionVwapPoint] = []
    newest_ts: Optional[float] = None
    for b in bars:
        ts = to_epoch_secs(b.get("ts"))
        if ts is None:
            continue
        if ts > dts:
            # incomplete bar (close after decision) -> fail closed
            return SessionVwapSeries(session_label=label, issue="INCOMPLETE",
                                     n_excluded_prior_day=excluded)
        if ts < start:
            excluded += 1
            continue
        close = b.get("close")
        vol = b.get("volume")
        try:
            close_f = float(close)
            vol_f = float(vol) if vol is not None else 0.0
        except (TypeError, ValueError):
            return SessionVwapSeries(session_label=label, issue="BAD_SAMPLE",
                                     n_excluded_prior_day=excluded)
        if not math.isfinite(close_f) or close_f <= 0 or vol_f <= 0:
            return SessionVwapSeries(session_label=label, issue="ZERO_VOLUME",
                                     n_excluded_prior_day=excluded)
        cum_pv += close_f * vol_f
        cum_vol += vol_f
        points.append(SessionVwapPoint(ts=ts, vwap=cum_pv / cum_vol, cum_volume=cum_vol))
        newest_ts = ts if newest_ts is None else max(newest_ts, ts)

    if not points:
        return SessionVwapSeries(session_label=label, issue="NO_SAMPLES",
                                 n_excluded_prior_day=excluded)
    if newest_ts is not None and (dts - newest_ts) > max_bar_gap_secs:
        return SessionVwapSeries(session_label=label, issue="STALE",
                                 points=points, n_excluded_prior_day=excluded)
    return SessionVwapSeries(session_label=label, points=points,
                             n_excluded_prior_day=excluded)


def compute_session_vwap(
    bars: Sequence[dict[str, Any]],
    decision_ts: Any,
    *,
    max_bar_gap_secs: float = 2.0 * BAR_5M_SECS,
) -> tuple[Optional[float], SessionVwapSeries]:
    """Latest cumulative session VWAP (or None when the series is fail-closed)."""
    series = compute_session_vwap_series(bars, decision_ts,
                                         max_bar_gap_secs=max_bar_gap_secs)
    if series.issue is not None or not series.points:
        return None, series
    return series.points[-1].vwap, series


# ── VWAP slope (spec section 2) ───────────────────────────────────────────

def compute_vwap_slope(vwap_t: Optional[float], vwap_t_minus_2dt: Optional[float],
                       delta_secs: float = DEFAULT_SLOPE_DELTA_SECS) -> Optional[float]:
    """Slope = (VWAP_t - VWAP_{t-2dt}) / (2dt), units pts/sec. None if missing."""
    if vwap_t is None or vwap_t_minus_2dt is None or delta_secs <= 0:
        return None
    try:
        return (float(vwap_t) - float(vwap_t_minus_2dt)) / (2.0 * float(delta_secs))
    except (TypeError, ValueError):
        return None


def slope_from_series(series: SessionVwapSeries, decision_ts: Any,
                      delta_secs: float = DEFAULT_SLOPE_DELTA_SECS) -> Optional[float]:
    """Slope at decision_ts: (vwap(newest) - vwap(at decision_ts - delta_secs)) / delta_secs.

    Both VWAP references must come from COMPLETED bars in the same session.
    None when the series is fail-closed or the 2-bar-ago reference is absent
    (fresh session / warmup — spec: slope must be confirmed, else BLOCK).
    """
    dts = to_epoch_secs(decision_ts)
    if dts is None or series.issue is not None or len(series.points) < 2:
        return None
    newest = series.points[-1]
    cutoff = dts - delta_secs
    prev = None
    for p in series.points:
        if p.ts <= cutoff:
            prev = p
        else:
            break
    if prev is None:
        return None
    return compute_vwap_slope(newest.vwap, prev.vwap, delta_secs=delta_secs)


# ── ATR-normalized distance / overextension (spec section 2) ──────────────

def atr_normalized_distance(price: Optional[float], vwap: Optional[float],
                            atr_15m: Optional[float]) -> Optional[float]:
    """|price - vwap| / ATR_15m. None when any input is missing/non-positive."""
    if price is None or vwap is None or atr_15m is None:
        return None
    try:
        p, v, a = float(price), float(vwap), float(atr_15m)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(p) and math.isfinite(v) and math.isfinite(a)) or a <= 0:
        return None
    return abs(p - v) / a


def is_overextended(distance: Optional[float],
                    overextend_atr_mult: float = DEFAULT_OVEREXTEND_ATR_MULT) -> bool:
    """True when |P - VWAP| exceeds overextend_atr_mult * ATR_15m (spec Q5)."""
    if distance is None:
        return False
    return float(distance) > float(overextend_atr_mult)


# ── Bar aggregation (15m / 60m from completed 5m bars) ────────────────────

def aggregate_completed_bars(
    bars_5m: Sequence[dict[str, Any]],
    decision_ts: Any,
    target_secs: float = BAR_15M_SECS,
) -> list[dict[str, Any]]:
    """Aggregate COMPLETED 5m bars into completed target bars (15m/60m).

    A 5m bar with close C spans [C-300, C); its target bucket is
    floor((C-300)/target_secs)*target_secs. A bucket is COMPLETE only when
    its close (bucket_start + target_secs) <= decision_ts AND it contains all
    target_secs/300 sub-bars (>=6 for 15m, >=12 for 60m). Buckets with gaps
    are dropped (fail-closed: an aggregated bar with missing volume is not a
    completed bar). Returns newest-first-free list (oldest first), each with
    keys ts (close), open, high, low, close, volume.
    """
    dts = to_epoch_secs(decision_ts)
    if dts is None:
        return []
    buckets: dict[int, dict[str, Any]] = {}
    for b in bars_5m:
        ts = to_epoch_secs(b.get("ts"))
        if ts is None or ts > dts:
            continue  # incomplete bar ignored
        sub = 300.0
        open_ts = ts - sub
        bucket_start = math.floor(open_ts / target_secs) * target_secs
        bucket_close = bucket_start + target_secs
        if bucket_close > dts:
            continue  # bucket not yet complete
        o = float(b.get("open") if b.get("open") is not None else b.get("close"))
        h = float(b.get("high") if b.get("high") is not None else b.get("close"))
        l = float(b.get("low") if b.get("low") is not None else b.get("close"))
        c = float(b.get("close"))
        v = float(b.get("volume") or 0.0)
        acc = buckets.get(bucket_start)
        if acc is None:
            buckets[bucket_start] = {"ts": bucket_close, "open": o, "high": h,
                                     "low": l, "close": c, "volume": v,
                                     "_n": 1}
        else:
            acc["high"] = max(acc["high"], h)
            acc["low"] = min(acc["low"], l)
            acc["close"] = c
            acc["volume"] += v
            acc["_n"] += 1
    need = int(target_secs / 300.0)
    out = []
    for bucket_start in sorted(buckets):
        acc = buckets[bucket_start]
        if acc["_n"] >= need:
            acc.pop("_n")
            out.append(acc)
    return out


# ── Tier classifiers ──────────────────────────────────────────────────────

def classify_60m_regime(bars_60m: Sequence[dict[str, Any]],
                        roc_threshold: float = DEFAULT_REGIME_ROC_THRESHOLD) -> Regime60m:
    """Tier 1: macro regime from completed 60m bars (>= 2 closes)."""
    closes = [float(b["close"]) for b in bars_60m
              if b.get("close") is not None]
    closes = [c for c in closes if math.isfinite(c) and c > 0]
    if len(closes) < 2:
        return Regime60m.UNKNOWN
    base = closes[-2]
    roc = (closes[-1] - base) / base
    if roc >= roc_threshold:
        return Regime60m.BULLISH_TREND
    if roc <= -roc_threshold:
        return Regime60m.BEARISH_TREND
    return Regime60m.RANGING


def classify_15m_direction(bars_15m: Sequence[dict[str, Any]],
                           flat_roc: float = DEFAULT_15M_FLAT_ROC) -> TrendDirection:
    """Tier 2: raw direction of the last two completed 15m closes."""
    closes = [float(b["close"]) for b in bars_15m
              if b.get("close") is not None]
    closes = [c for c in closes if math.isfinite(c) and c > 0]
    if len(closes) < 2:
        return TrendDirection.UNKNOWN
    roc = (closes[-1] - closes[-2]) / closes[-2]
    if roc >= flat_roc:
        return TrendDirection.BULLISH
    if roc <= -flat_roc:
        return TrendDirection.BEARISH
    return TrendDirection.CHOP


def signal_15m_verdict(raw: TrendDirection, retained: TrendDirection) -> Signal15m:
    """Map the raw 15m direction against the retained-leg direction (spec Tier 2)."""
    if raw in (TrendDirection.UNKNOWN,):
        return Signal15m.UNKNOWN
    if raw == TrendDirection.CHOP:
        return Signal15m.NEUTRAL
    if retained in (TrendDirection.BULLISH, TrendDirection.BEARISH) and raw == retained:
        return Signal15m.CONFIRMED_CONTINUATION
    if retained in (TrendDirection.BULLISH, TrendDirection.BEARISH):
        return Signal15m.REVERSAL
    return Signal15m.NEUTRAL


def confirm_5m_direction(bars_5m: Sequence[dict[str, Any]],
                         direction: TrendDirection,
                         n: int = 2) -> int:
    """Tier 4: count of trailing completed 5m bars confirming `direction`.

    Returns the number of consecutive completed bars (from the newest back)
    whose closes move in `direction` (c[i] > c[i-1] for BULLISH,
    c[i] < c[i-1] for BEARISH). CHOP/UNKNOWN direction -> 0.
    """
    if direction not in (TrendDirection.BULLISH, TrendDirection.BEARISH):
        return 0
    closes = [float(b["close"]) for b in bars_5m if b.get("close") is not None]
    closes = [c for c in closes if math.isfinite(c) and c > 0]
    if len(closes) < 2:
        return 0
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if direction == TrendDirection.BULLISH and closes[i] > closes[i - 1]:
            count += 1
        elif direction == TrendDirection.BEARISH and closes[i] < closes[i - 1]:
            count += 1
        else:
            break
        if count >= n:
            break
    return count


# ── Per-leg VWAP state ────────────────────────────────────────────────────

@dataclass(frozen=True)
class LegVwapState:
    leg: str                     # "NEAR" | "FAR"
    side: Optional[str]          # position side LONG/SHORT (entry side)
    price: Optional[float]       # live quote
    vwap: Optional[float]        # session VWAP (None when fail-closed)
    vwap_source: LegVwapSource   # how the value was obtained
    slope: Optional[float]       # pts/sec (None when unavailable)
    atr_15m: Optional[float]
    atr_normalized_distance: Optional[float]
    is_overextended: Optional[bool]
    aligned: Optional[bool]      # None when vwap/slope missing (cannot judge)
    issue: Optional[str]         # MISSING_VWAP / STALE / ZERO_VOLUME / SLOPE_UNAVAILABLE / ...


# ── Verdict ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HvwapCandidateVerdict:
    decision_ts: str
    session_label: str
    status: HvwapStatus
    block_reason: Optional[str]
    regime_60m: Regime60m
    signal_15m: Signal15m
    consecutive_confirmed_bars: int
    bars_complete: bool
    session_boundary_ok: bool
    n_completed_5m_bars: int
    near: LegVwapState
    far: LegVwapState
    retained_direction: Optional[str]
    hypothetical_release_leg: Optional[str]
    position_phase: str
    quote_age_ms: Optional[float]
    max_quote_age_ms: float

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["regime_60m"] = self.regime_60m.value
        d["signal_15m"] = self.signal_15m.value
        d["near"]["vwap_source"] = self.near.vwap_source.value
        d["far"]["vwap_source"] = self.far.vwap_source.value
        return d


def _leg_eval(leg: str, side: Optional[str], price: Optional[float],
              vwap: Optional[float], source: LegVwapSource,
              series: Optional[SessionVwapSeries],
              slope: Optional[float], atr_15m: Optional[float],
              eps: float, slope_eps: float,
              ) -> tuple[LegVwapState, Optional[str]]:
    """Evaluate one leg's VWAP filter. Returns (state, first_issue)."""
    issue = None
    if vwap is None:
        issue = "MISSING_VWAP"
        if series is not None and series.issue:
            issue = series.issue
        return LegVwapState(leg=leg, side=side, price=price, vwap=None,
                            vwap_source=source, slope=None, atr_15m=atr_15m,
                            atr_normalized_distance=None, is_overextended=None,
                            aligned=None, issue=issue), issue
    dist = atr_normalized_distance(price, vwap, atr_15m)
    overext = is_overextended(dist) if dist is not None else None
    aligned: Optional[bool] = None
    if price is not None and slope is not None and atr_15m is not None:
        if str(side).upper() == "LONG":
            aligned = (float(price) > float(vwap) + eps and slope > slope_eps)
        elif str(side).upper() == "SHORT":
            aligned = (float(price) < float(vwap) - eps and slope < -slope_eps)
        else:
            aligned = False
            issue = "ENTRY_SIDES_UNKNOWN"
    elif price is None:
        issue = "MISSING_QUOTE"
    elif slope is None:
        issue = "SLOPE_UNAVAILABLE"
    return LegVwapState(leg=leg, side=side, price=price, vwap=vwap,
                        vwap_source=source, slope=slope, atr_15m=atr_15m,
                        atr_normalized_distance=dist, is_overextended=overext,
                        aligned=aligned, issue=issue), issue


def evaluate_hvwap_candidate(
    *,
    decision_ts: Any,
    near_bars: Sequence[dict[str, Any]],
    far_bars: Sequence[dict[str, Any]],
    near_price: Optional[float],
    far_price: Optional[float],
    near_side: Optional[str],
    far_side: Optional[str],
    atr_15m: Optional[float],
    quote_age_ms: Optional[float] = None,
    near_quote_age_ms: Optional[float] = None,
    far_quote_age_ms: Optional[float] = None,
    near_vwap_now: Optional[float] = None,
    far_vwap_now: Optional[float] = None,
    max_quote_age_ms: float = DEFAULT_MAX_QUOTE_AGE_MS,
    max_bar_gap_secs: float = 2.0 * BAR_5M_SECS,
    overextend_atr_mult: float = DEFAULT_OVEREXTEND_ATR_MULT,
    deadband_atr_frac: float = DEFAULT_DEADBAND_ATR_FRAC,
    tick_size: float = DEFAULT_TICK_SIZE,
    slope_delta_secs: float = DEFAULT_SLOPE_DELTA_SECS,
    regime_roc_threshold: float = DEFAULT_REGIME_ROC_THRESHOLD,
    position_phase: str = "SPREAD",
) -> HvwapCandidateVerdict:
    """Top-level Hierarchical VWAP candidate arbitration (PURE, telemetry-only).

    Returns an immutable verdict. NEVER emits orders, never gates the
    baseline decision arm, never raises on bad data (fail-closed -> UNKNOWN).

    Inputs:
      decision_ts     as-of boundary; bars with close > decision_ts are
                      incomplete and fail closed.
      near_bars/far_bars  completed per-leg 5m bars {ts: close, open, high,
                      low, close, volume}. Volume is REQUIRED for the leg's
                      own session VWAP (spec 1.4). far volume missing ->
                      far leg MISSING_VWAP (fail-closed).
      near_vwap_now/far_vwap_now  optional precomputed session VWAP values;
                      when provided they are used as the leg's session VWAP
                      (source=PROVIDED) instead of the samples path.
      near_side/far_side  ENTRY sides; used for retained direction +
                      hypothetical release leg via counter_trend_leg_from_sides.
      quote ages       freshness gates (ms); the strictest (max) applies.
    """
    dts = to_epoch_secs(decision_ts)
    if dts is None:
        return _unknown_verdict(decision_ts, "BAD_DECISION_TS")
    try:
        label, _start = vwap_session_bounds(dts)
    except ValueError:
        return _unknown_verdict(decision_ts, "BAD_DECISION_TS")

    # bar completeness: every provided bar must be at/before decision_ts
    for b in list(near_bars) + list(far_bars):
        ts = to_epoch_secs(b.get("ts"))
        if ts is not None and ts > dts:
            return _unknown_verdict(decision_ts, "INCOMPLETE_BARS",
                                    session_label=label, quote_age_ms=quote_age_ms,
                                    max_quote_age_ms=max_quote_age_ms)
    bars_complete = True

    # per-leg session VWAP series
    near_series = compute_session_vwap_series(near_bars, dts,
                                              max_bar_gap_secs=max_bar_gap_secs)
    far_series = compute_session_vwap_series(far_bars, dts,
                                             max_bar_gap_secs=max_bar_gap_secs)
    near_vwap = None
    far_vwap = None
    near_source = LegVwapSource.MISSING
    far_source = LegVwapSource.MISSING
    if near_vwap_now is not None and math.isfinite(float(near_vwap_now)) and float(near_vwap_now) > 0:
        near_vwap = float(near_vwap_now)
        near_source = LegVwapSource.PROVIDED
    elif near_series.issue is None and near_series.points:
        near_vwap = near_series.points[-1].vwap
        near_source = LegVwapSource.SESSION_ACCUMULATED
    if far_vwap_now is not None and math.isfinite(float(far_vwap_now)) and float(far_vwap_now) > 0:
        far_vwap = float(far_vwap_now)
        far_source = LegVwapSource.PROVIDED
    elif far_series.issue is None and far_series.points:
        far_vwap = far_series.points[-1].vwap
        far_source = LegVwapSource.SESSION_ACCUMULATED

    # slope from each leg's session series (2 completed 5m bars apart)
    near_slope = slope_from_series(near_series, dts, delta_secs=slope_delta_secs)
    far_slope = slope_from_series(far_series, dts, delta_secs=slope_delta_secs)

    # freshness
    ages = [a for a in (near_quote_age_ms, far_quote_age_ms, quote_age_ms)
            if a is not None]
    eff_age = max(ages) if ages else None
    if eff_age is not None and float(eff_age) > float(max_quote_age_ms):
        return _unknown_verdict(decision_ts, "STALE_QUOTE", session_label=label,
                                quote_age_ms=eff_age, max_quote_age_ms=max_quote_age_ms)

    # ATR required (fail-closed: cannot verify overextension / deadband)
    if atr_15m is None:
        return _unknown_verdict(decision_ts, "MISSING_ATR", session_label=label,
                                quote_age_ms=eff_age, max_quote_age_ms=max_quote_age_ms)
    try:
        atr_f = float(atr_15m)
    except (TypeError, ValueError):
        atr_f = 0.0
    if atr_f <= 0:
        return _unknown_verdict(decision_ts, "MISSING_ATR", session_label=label,
                                quote_age_ms=eff_age, max_quote_age_ms=max_quote_age_ms)

    eps = max(tick_size, deadband_atr_frac * atr_f)
    slope_eps = eps / (BAR_5M_SECS)   # epsilon per 5 minutes, in pts/sec

    near_state, near_issue = _leg_eval("NEAR", near_side, near_price, near_vwap,
                                       near_source, near_series, near_slope,
                                       atr_f, eps, slope_eps)
    far_state, far_issue = _leg_eval("FAR", far_side, far_price, far_vwap,
                                     far_source, far_series, far_slope,
                                     atr_f, eps, slope_eps)

    # retained direction + hypothetical release leg (pure, never PNL)
    retained, release_leg = counter_trend_leg_from_sides(near_side, far_side)
    retained_dir = None
    if retained is not None:
        retained_dir = "BULLISH" if retained.value == "LONG" else "BEARISH"
        retained_td = TrendDirection(retained_dir)
    else:
        retained_td = TrendDirection.UNKNOWN

    # Tier 1: 60m regime (aggregated from completed 5m bars)
    bars_60m = aggregate_completed_bars(near_bars, dts, BAR_60M_SECS)
    regime = classify_60m_regime(bars_60m, roc_threshold=regime_roc_threshold)

    # Tier 2: 15m direction (aggregated from completed 5m bars)
    bars_15m = aggregate_completed_bars(near_bars, dts, BAR_15M_SECS)
    raw_15m = classify_15m_direction(bars_15m)
    sig15 = signal_15m_verdict(raw_15m, retained_td)

    # Tier 4: 5m confirmation (session bars only, completed)
    session_5m = [b for b in near_bars
                  if to_epoch_secs(b.get("ts")) is not None
                  and _start <= to_epoch_secs(b.get("ts")) <= dts]
    confirmed = confirm_5m_direction(session_5m, retained_td, n=2)
    n_completed = len(session_5m)

    # ── Arbitration (spec flowchart) ──
    reason: Optional[str] = None
    status = HvwapStatus.ALIGNED_PASS

    if position_phase != "SPREAD":
        status, reason = HvwapStatus.BLOCK, "NOT_IN_SPREAD"
    elif retained is None or release_leg is None:
        status, reason = HvwapStatus.BLOCK, "ENTRY_SIDES_UNKNOWN"
    elif near_issue is not None or far_issue is not None:
        status, reason = HvwapStatus.UNKNOWN, (near_issue or far_issue)
    elif regime == Regime60m.UNKNOWN:
        status, reason = HvwapStatus.UNKNOWN, "MISSING_60M_BARS"
    elif regime == Regime60m.RANGING:
        status, reason = HvwapStatus.BLOCK, "REGIME_BLOCK"
    elif sig15 in (Signal15m.UNKNOWN,):
        status, reason = HvwapStatus.UNKNOWN, "MISSING_15M_BARS"
    elif sig15 == Signal15m.REVERSAL:
        status, reason = HvwapStatus.BLOCK, "SIGNAL_15M_BLOCK"
    elif sig15 == Signal15m.NEUTRAL:
        status, reason = HvwapStatus.BLOCK, "SIGNAL_15M_BLOCK"
    elif confirmed < 2:
        status, reason = HvwapStatus.BLOCK, "INSUFFICIENT_CONFIRMATION"
    elif near_state.aligned is not True or far_state.aligned is not True:
        status, reason = HvwapStatus.BLOCK, "FILTER_REJECT"
    else:
        # overextension check on the RETAINED leg (spec Q5 -> HOLD)
        retained_state = near_state if str(release_leg).upper() == "FAR" else far_state
        if retained_state.is_overextended:
            status, reason = HvwapStatus.HOLD, "OVEREXTENDED_HOLD"

    return HvwapCandidateVerdict(
        decision_ts=str(decision_ts),
        session_label=label,
        status=status,
        block_reason=reason,
        regime_60m=regime,
        signal_15m=sig15,
        consecutive_confirmed_bars=confirmed,
        bars_complete=bars_complete,
        session_boundary_ok=True,
        n_completed_5m_bars=n_completed,
        near=near_state,
        far=far_state,
        retained_direction=retained_dir,
        hypothetical_release_leg=str(release_leg) if release_leg else None,
        position_phase=position_phase,
        quote_age_ms=eff_age,
        max_quote_age_ms=float(max_quote_age_ms),
    )


def _unknown_verdict(decision_ts: Any, reason: str, *,
                     session_label: str = "", quote_age_ms: Optional[float] = None,
                     max_quote_age_ms: float = DEFAULT_MAX_QUOTE_AGE_MS,
                     phase: str = "SPREAD") -> HvwapCandidateVerdict:
    return HvwapCandidateVerdict(
        decision_ts=str(decision_ts),
        session_label=session_label,
        status=HvwapStatus.UNKNOWN,
        block_reason=reason,
        regime_60m=Regime60m.UNKNOWN,
        signal_15m=Signal15m.UNKNOWN,
        consecutive_confirmed_bars=0,
        bars_complete=False,
        session_boundary_ok=(session_label != ""),
        n_completed_5m_bars=0,
        near=LegVwapState(leg="NEAR", side=None, price=None, vwap=None,
                          vwap_source=LegVwapSource.MISSING, slope=None,
                          atr_15m=None, atr_normalized_distance=None,
                          is_overextended=None, aligned=None, issue=reason),
        far=LegVwapState(leg="FAR", side=None, price=None, vwap=None,
                         vwap_source=LegVwapSource.MISSING, slope=None,
                         atr_15m=None, atr_normalized_distance=None,
                         is_overextended=None, aligned=None, issue=reason),
        retained_direction=None,
        hypothetical_release_leg=None,
        position_phase=phase,
        quote_age_ms=quote_age_ms,
        max_quote_age_ms=float(max_quote_age_ms),
    )


# ── Paired counterfactual outcomes (PURE, isolated) ───────────────────────

@dataclass(frozen=True)
class CounterfactualOutcomes:
    """Paired counterfactual fields for one candidate release decision.

    cf_directional_total_pts  = realized released-leg PnL + unrealized
                                retained-leg PnL (the candidate path)
    cf_hold_spread_pts        = both legs held to now (baseline path)
    cf_alpha_pts              = directional_total - hold_spread
    cf_net_alpha_twd          = alpha * point_value - release friction

    All PnL in points; friction is an explicit input so the caller controls
    the fee/tax model. Never mutates strategy state (pure).
    """
    release_leg: str
    release_price: float
    cf_released_leg_realized_pnl_pts: float
    cf_retained_leg_unrealized_pnl_pts: float
    cf_directional_total_pnl_pts: float
    cf_hold_spread_pnl_pts: float
    cf_alpha_pts: float
    cf_release_friction_twd: float
    cf_net_alpha_twd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _leg_pnl_pts(side: Optional[str], entry: Optional[float], mark: Optional[float]) -> Optional[float]:
    if side is None or entry is None or mark is None:
        return None
    try:
        e, m = float(entry), float(mark)
    except (TypeError, ValueError):
        return None
    return (m - e) if str(side).upper() == "LONG" else (e - m)


def compute_counterfactual_outcomes(
    *,
    near_side: Optional[str],
    far_side: Optional[str],
    near_entry: Optional[float],
    far_entry: Optional[float],
    release_leg: Optional[str],
    release_price: Optional[float],
    near_now: Optional[float],
    far_now: Optional[float],
    point_value: float = 10.0,
    release_friction_twd: float = 0.0,
) -> Optional[CounterfactualOutcomes]:
    """Paired counterfactual PnL for the hypothetical release (PURE, isolated).

    Returns None when any required input is missing/invalid (fail-closed).
    The candidate path releases `release_leg` at `release_price` and holds
    the retained leg; the baseline path holds both legs to now.
    """
    if release_leg not in ("NEAR", "FAR"):
        return None
    if release_price is None or near_now is None or far_now is None:
        return None
    near_pts = _leg_pnl_pts(near_side, near_entry, near_now)
    far_pts = _leg_pnl_pts(far_side, far_entry, far_now)
    if near_pts is None or far_pts is None:
        return None
    try:
        rel_price = float(release_price)
    except (TypeError, ValueError):
        return None
    if release_leg == "NEAR":
        released_realized = _leg_pnl_pts(near_side, near_entry, rel_price)
        retained_unrealized = far_pts
    else:
        released_realized = _leg_pnl_pts(far_side, far_entry, rel_price)
        retained_unrealized = near_pts
    if released_realized is None or retained_unrealized is None:
        return None
    directional = released_realized + retained_unrealized
    hold_spread = near_pts + far_pts
    alpha = directional - hold_spread
    return CounterfactualOutcomes(
        release_leg=release_leg,
        release_price=rel_price,
        cf_released_leg_realized_pnl_pts=round(released_realized, 4),
        cf_retained_leg_unrealized_pnl_pts=round(retained_unrealized, 4),
        cf_directional_total_pnl_pts=round(directional, 4),
        cf_hold_spread_pnl_pts=round(hold_spread, 4),
        cf_alpha_pts=round(alpha, 4),
        cf_release_friction_twd=round(float(release_friction_twd), 4),
        cf_net_alpha_twd=round(alpha * float(point_value) - float(release_friction_twd), 4),
    )


# ── DataFrame adapter (wiring convenience) ────────────────────────────────

def completed_bars_from_df(df: Any, *,
                           bar_secs: float = BAR_5M_SECS,
                           index_is_start: bool = True,
                           max_bars: int = 320,
                           ) -> list[dict[str, Any]]:
    """Convert a pandas OHLCV frame (index = bucket timestamps) into completed
    bar dicts {ts: close-time, open, high, low, close, volume}.

    When index_is_start is True the index is the bucket START; the returned
    ts is the CLOSE time (start + bar_secs) — the moment the bar completes.
    Rows with unparseable timestamps or non-finite closes are skipped.
    """
    if df is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        rows = df.tail(max_bars)
        for idx, row in rows.iterrows():
            ts = to_epoch_secs(idx)
            if ts is None:
                continue
            if index_is_start:
                ts = ts + float(bar_secs)
            close = row.get("Close", row.get("close"))
            try:
                c = float(close)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(c) or c <= 0:
                continue
            def _f(key: str, default: float) -> float:
                v = row.get(key)
                try:
                    return float(v) if v is not None else default
                except (TypeError, ValueError):
                    return default
            vol = row.get("Volume", row.get("volume"))
            try:
                v = float(vol) if vol is not None else 0.0
            except (TypeError, ValueError):
                v = 0.0
            out.append({"ts": ts, "open": _f("Open", c), "high": _f("High", c),
                        "low": _f("Low", c), "close": c, "volume": v})
    except Exception:
        return []
    return out
