#!/usr/bin/env python3
"""As-of-only MTS 2.0 trend-release counterfactual replay.

Input files contain irregular 1-minute OHLCV observations.  All decisions are
made from completed 5-minute bars; the 60-minute regime and 15-minute
confirmation are also completed-block signals.  A confirmed signal executes
on the next available completed 5-minute bar, never on the signal bar.

Phase 2 (2026-08-22): three FROZEN replay variants share the same cost model
(BROKER_FEE/TAX_RATE/POINT_VALUE) and the same fill/execution semantics (next
completed bar close).  All variant parameters are pre-registered in
VARIANT_PARAMS at the top of this module and are immutable (MappingProxyType):
  - STRICT            : 2-of-3 where a CHOP regime or CHOP exit VETOES
                        (matches the authoritative spec's CHOP->BLOCK matrix).
  - TOLERANT          : current P1 behavior — CHOP regime/exit does NOT veto;
                        only a genuinely opposite (divergent) block or UNKNOWN
                        blocks.
  - TOLERANT_VELOCITY : TOLERANT plus a spread-velocity confirmation on the
                        last completed 15m block (slope of near-far spread
                        over completed 5m closes, as-of only).  Flat/missing
                        velocity blocks with VELOCITY_FLAT; a velocity whose
                        sign opposes the expected trend blocks with
                        VELOCITY_OPPOSITE.

Phase 3 (2026-08-22): multi-window walk-forward.  run_walk_forward() splits
the fills-covered dates into contiguous windows, designates the LAST
``n_oos_windows`` windows as out-of-sample (OOS), and records earlier windows
only as observation.  There is NO parameter optimization on the evaluation
window — parameters are pre-registered and fixed (no_eval_window_tuning=True).
OOS is ROBUST iff (a) total OOS eligible >= OOS_MIN_ELIGIBLE (30),
(b) at least OOS_MIN_WINDOWS (2) OOS windows, (c) aggregate OOS pnl > OOS
baseline pnl under the SAME pessimistic cost model, (d) OOS release count > 0,
and (e) OOS coverage >= 0.9; otherwise HOLD with status
RESEARCH_INSUFFICIENT_OOS_SAMPLE when the OOS sample itself is insufficient.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType

# 2026-08-22 MTS2: when run as a script, sys.path[0] is scripts/research/
# (not the repo root), so the real trend modules would fail to import and
# the fallback stub would silently mask every signal. Insert the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

HAS_MODULES = True
try:
    from strategies.plugins.futures.active.mts_trend_signal_adapter import (
        TrendDirection, compute_adl_snr, adl_signal_state, arbitrate_trend,
    )
    from strategies.plugins.futures.active.mts_renko_signal import compute_renko, renko_signal_state
    from strategies.plugins.futures.active.mts_micro_vwap import compute_micro_vwap, vwap_signal_state
except Exception:  # pragma: no cover
    HAS_MODULES = False
    from enum import Enum
    class TrendDirection(str, Enum):
        BULLISH = "BULLISH"; BEARISH = "BEARISH"; CHOP = "CHOP"; UNKNOWN = "UNKNOWN"

# ---- Shared pessimistic cost model (identical for EVERY variant) -----------
POINT_VALUE = 10.0
BROKER_FEE = 20.0
TAX_RATE = 2e-5
SHARED_COST = {"broker_fee": BROKER_FEE, "tax_rate": TAX_RATE, "point_value": POINT_VALUE}

MIN_ELIGIBLE_FOR_APPROVAL = 30
OOS_MIN_ELIGIBLE = 30
OOS_MIN_WINDOWS = 2
ARMS = ("BASELINE_SINGLE_LEG_RELEASE", "TREND_CONFIRMED_RELEASE", "NO_REVT")
SELF_PATH = Path(__file__)

# ---- Phase 2: frozen, pre-registered variant parameters -------------------
# Every variant shares SHARED_COST and identical fill/execution semantics;
# the params below only encode the confirmation-gate behavior.  Immutable on
# purpose: variants are frozen research artifacts, never tuned per window.
_VARIANT_PARAM_DEFAULTS = {
    "STRICT": {
        "chop_vetoes": True,          # CHOP regime/exit vetoes (spec CHOP->BLOCK)
        "velocity_check": False,
        "velocity_min_abs_slope_pts": None,
        "description": "2-of-3; CHOP regime or CHOP exit vetoes (spec matrix)",
    },
    "TOLERANT": {
        "chop_vetoes": False,         # P1 behavior: CHOP does not veto
        "velocity_check": False,
        "velocity_min_abs_slope_pts": None,
        "description": "P1 loose 2-of-3; only opposite/UNKNOWN blocks",
    },
    "TOLERANT_VELOCITY": {
        "chop_vetoes": False,         # same gate as TOLERANT
        "velocity_check": True,       # plus 15m spread-velocity confirmation
        "velocity_min_abs_slope_pts": 0.05,  # |slope| below this => VELOCITY_FLAT
        "description": "TOLERANT + 15m near-far spread velocity agrees with trend",
    },
}
VARIANT_PARAMS = MappingProxyType(
    {name: MappingProxyType(params) for name, params in _VARIANT_PARAM_DEFAULTS.items()}
)
VALID_VARIANTS = tuple(VARIANT_PARAMS)


def _variant_params(variant: str) -> dict:
    """Plain-dict snapshot of a variant's pre-registered params (JSON-safe)."""
    return dict(VARIANT_PARAMS[variant])


def cost(entry_px: float, exit_px: float) -> float:
    return 2.0 * BROKER_FEE + (entry_px + exit_px) * POINT_VALUE * TAX_RATE


def leg_pnl(entry_px: float, exit_px: float, side: str) -> float:
    sign = 1.0 if str(side).upper() == "LONG" else -1.0
    return (exit_px - entry_px) * sign * POINT_VALUE - cost(entry_px, exit_px)


def discover_fills_log(root: str) -> Path | None:
    root_p = Path(root)
    cands = [root_p / "logs" / "mts_trade_fills.jsonl"]
    if (root_p / "data").exists():
        cands += sorted((root_p / "data").glob("**/mts_trade_fills.jsonl"))
    present = [c for c in cands if c.exists() and c.stat().st_size > 0]
    return max(present, key=lambda p: p.stat().st_size) if present else None


def discover_bar_files(root: str, leg: str) -> list[Path]:
    return sorted(Path(root).glob(f"data/tmf_{leg}_*.csv"))


def load_fills(path: str) -> list[dict]:
    out = []
    if not Path(path).exists(): return out
    for line in Path(path).read_text().splitlines():
        try: out.append(json.loads(line))
        except json.JSONDecodeError: pass
    return out


def load_bars(files: list[Path], leg: str) -> dict[str, dict]:
    out = {}
    for fp in sorted(files, key=lambda p: p.name):
        if not fp.exists(): continue
        with open(fp, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    ts = row["ts"].strip().replace("T", " ")
                    out[ts] = {"ts": ts, "open": float(row["Open"]), "high": float(row["High"]),
                               "low": float(row["Low"]), "close": float(row["Close"]),
                               "volume": float(row.get("Volume") or 0), "leg": leg}
                except (KeyError, ValueError, TypeError): pass
    return out


def build_episodes(fills: list[dict]) -> list[dict]:
    by = {}
    for f in fills: by.setdefault(str(f.get("trade_id") or "unknown"), []).append(f)
    out = []
    for tid in sorted(by):
        ep = {"trade_id": tid, "entry_near": None, "entry_far": None, "release": None, "exit": None}
        for f in by[tid]:
            if f.get("fill_type") == "ENTRY" and f.get("leg") == "NEAR": ep["entry_near"] = f
            elif f.get("fill_type") == "ENTRY" and f.get("leg") == "FAR": ep["entry_far"] = f
            elif f.get("fill_type") == "RELEASE": ep["release"] = f
            elif f.get("fill_type") == "EXIT": ep["exit"] = f
        out.append(ep)
    return out


def normalize_ts(ts: str) -> str:
    return str(ts).strip().replace("T", " ")


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(normalize_ts(ts))


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def bar_at_or_before(keys: list[str], bars: dict[str, dict], ts: str) -> dict | None:
    i = bisect.bisect_right(keys, normalize_ts(ts)) - 1
    return bars[keys[i]] if i >= 0 else None


def _completed_bars(keys: list[str], bars: dict[str, dict], leg: str,
                    ts_from: str, ts_to: str, minutes: int) -> list[dict]:
    """Aggregate observations into full, closed minute blocks as-of ts_to."""
    start = _dt(ts_from).replace(second=0, microsecond=0)
    end = _dt(ts_to)
    groups = {}
    for k in keys:
        t = _dt(k)
        if t < start or t > end: continue
        block_start = t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)
        close_time = block_start + timedelta(minutes=minutes - 1)
        if close_time > end: continue
        groups.setdefault(block_start, []).append(bars[k])
    result = []
    for block_start in sorted(groups):
        rows = groups[block_start]
        # Completion is determined by the block close boundary.  The source
        # can legitimately omit inactive minutes; missing observations are not
        # forward-filled or invented.
        result.append({"ts": _fmt(block_start + timedelta(minutes=minutes - 1)),
                       "open": rows[0]["open"], "high": max(r["high"] for r in rows),
                       "low": min(r["low"] for r in rows), "close": rows[-1]["close"],
                       "volume": sum(r["volume"] for r in rows), "leg": leg,
                       "asof_ts": _fmt(block_start + timedelta(minutes=minutes - 1))})
    return result


def agg_5m(keys: list[str], bars_dict: dict[str, dict], leg: str,
           ts_from: str, ts_to: str) -> list[dict]:
    """Return chronological completed 5-minute OHLCV bars as-of ts_to."""
    return _completed_bars(keys, bars_dict, leg, ts_from, ts_to, 5)


def _atr(series: list[dict]) -> float:
    if not series: return 1.0
    prev, trs = series[0]["close"], []
    for b in series:
        trs.append(max(b["high"] - b["low"], abs(b["high"] - prev), abs(b["low"] - prev)))
        prev = b["close"]
    return (sum(trs) / len(trs)) or 1.0


def _vwap_samples(series: list[dict]) -> list[dict]:
    out = []
    for b in series:
        per = max(float(b.get("volume") or 0), 1.0) / 12
        for _ in range(12): out.append({"ts": b["ts"], "price": b["close"], "volume": per})
    return out[-900:]


def entry_trend_mapping(ep: dict) -> tuple[TrendDirection | None, str | None]:
    """Return retained-leg direction and counter-trend release leg from entries."""
    try:
        ns, fs = str(ep["entry_near"]["side"]).upper(), str(ep["entry_far"]["side"]).upper()
    except (TypeError, KeyError): return None, None
    if ns == "LONG" and fs == "SHORT": return TrendDirection.BULLISH, "FAR"
    if ns == "SHORT" and fs == "LONG": return TrendDirection.BEARISH, "NEAR"
    return None, None


def _trend_decision(decision_ts: str, near_series: list[dict],
                    expected: TrendDirection | None = None) -> dict:
    """Run the real pipeline on one immutable, completed-bar snapshot."""
    if not HAS_MODULES:
        return {"decision_ts": decision_ts, "direction": "CHOP", "confidence": 0.0,
                "pass_release": False, "block_reason": "MODULES_UNAVAILABLE",
                "signal_timestamps": {"adl": None, "renko": None, "vwap": None}}
    expected = expected or TrendDirection.UNKNOWN
    if expected == TrendDirection.UNKNOWN:
        return {"decision_ts": decision_ts, "direction": "CHOP", "confidence": 0.0,
                "pass_release": False, "block_reason": "INVALID_ENTRY_SIDE",
                "signal_timestamps": {"adl": None, "renko": None, "vwap": None}}
    bars = [{k: b[k] for k in ("high", "low", "close", "volume")} for b in near_series]
    adl = compute_adl_snr(decision_ts, bars[-12:], window_n=12)
    adl_ss = adl_signal_state(adl, expected)
    closes = [b["close"] for b in near_series]
    brick = max(_atr(near_series) * 0.5, 0.1)
    ren = compute_renko(decision_ts, closes, brick, seed_price=closes[0] if closes else 0)
    ren_ss = renko_signal_state(ren, expected)
    vw = compute_micro_vwap(decision_ts, _vwap_samples(near_series), atr_1m=max(_atr(near_series), .01))
    vw_ss = vwap_signal_state(vw, expected)
    d = arbitrate_trend(decision_ts, ren_ss, adl_ss, vw_ss, decision_max_quote_age_ms=0.0, window_max_quote_age_ms=0.0).to_dict()
    d["signal_timestamps"] = {"adl": decision_ts, "renko": decision_ts, "vwap": decision_ts}
    if not d.get("pass_release") and not d.get("block_reason"): d["block_reason"] = "UNKNOWN"
    return d


def _direction(block: dict) -> TrendDirection:
    if block["close"] > block["open"]: return TrendDirection.BULLISH
    if block["close"] < block["open"]: return TrendDirection.BEARISH
    return TrendDirection.CHOP


def _aggregate_completed_5m_blocks(five: list[dict], minutes: int) -> list[dict]:
    """Aggregate completed 5m bars into completed 15m/60m blocks."""
    groups = {}
    for b in five:
        t = _dt(b["ts"])
        start = t.replace(minute=(t.minute // minutes) * minutes, second=0, microsecond=0)
        close = start + timedelta(minutes=minutes - 1)
        if close > _dt(five[-1]["ts"]):
            continue
        groups.setdefault(start, []).append(b)
    out = []
    for start in sorted(groups):
        rows = sorted(groups[start], key=lambda x: x["ts"])
        # A completed higher-timeframe block needs every constituent 5m bar.
        if len(rows) < minutes // 5:
            continue
        out.append({"ts": _fmt(start + timedelta(minutes=minutes - 1)),
                    "open": rows[0]["open"], "high": max(x["high"] for x in rows),
                    "low": min(x["low"] for x in rows), "close": rows[-1]["close"],
                    "volume": sum(x["volume"] for x in rows)})
    return out


def _lin_slope(xs: list[float], ys: list[float]) -> float:
    """Least-squares slope over (x, y) samples; 0.0 when undefined (flat)."""
    n = len(xs)
    if n < 2: return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def _spread_velocity_15m(five_before: list[dict], far_keys: list[str] | None,
                         far_bars: dict[str, dict] | None,
                         expected: TrendDirection,
                         completed_15m: list[dict] | None = None) -> dict | None:
    """Slope of the near-far spread across the last COMPLETED 15m block.

    Uses ONLY completed 5m closes as-of the current decision bar (no
    lookahead): near closes come from ``five_before``; the matching far close
    is the far 1-min bar at-or-before each near 5m close timestamp.  The block
    bounds are taken from ``completed_15m`` (the harness's own completed-block
    aggregation) so the current, not-yet-completed quarter is never used.
    Returns None when fewer than 2 aligned points exist (missing velocity).
    """
    if not five_before or not far_keys or not far_bars:
        return None
    if completed_15m:
        t_close = _dt(completed_15m[-1]["ts"])
        t_start = t_close - timedelta(minutes=14)
    else:
        t_last = _dt(five_before[-1]["ts"])
        t_start = t_last.replace(minute=(t_last.minute // 15) * 15, second=0, microsecond=0)
        t_close = t_start + timedelta(minutes=14)
    rows = [b for b in five_before if t_start <= _dt(b["ts"]) <= t_close]
    xs, spreads = [], []
    t0 = _dt(rows[0]["ts"])
    for b in rows:
        fb = bar_at_or_before(far_keys, far_bars, b["ts"])
        if fb is None:
            continue
        xs.append((_dt(b["ts"]) - t0).total_seconds() / 60.0)
        spreads.append(b["close"] - fb["close"])
    if len(spreads) < 2:
        return None
    slope = _lin_slope(xs, spreads)
    # BULLISH = long near / short far -> spread (near-far) should widen (+slope).
    aligned = slope > 0.0 if expected == TrendDirection.BULLISH else slope < 0.0
    return {"slope": slope, "n": len(spreads), "aligned": aligned}


def walk_trend_confirmation(keys: list[str], bars: dict[str, dict],
                            ts_from: str, ts_to: str, expected: TrendDirection | None = None,
                            ep: dict | None = None, variant: str = "TOLERANT",
                            far_bars: dict[str, dict] | None = None,
                            far_keys: list[str] | None = None) -> dict | None:
    """Find earliest as-of decision where completed 60m, 15m and pipeline align.

    P0 (2026-08-22): pre-warm history — the 5m aggregation starts 60 minutes
    BEFORE entry so ADL/regime/15m have their completed blocks available at the
    first post-entry decision bar. Decision iteration still begins at the first
    bar whose asof_ts >= entry_ts (never fires on pre-entry bars).

    Phase 2 (2026-08-22): the confirmation gate honors the FROZEN ``variant``
    from VARIANT_PARAMS (STRICT / TOLERANT / TOLERANT_VELOCITY).
    """
    if variant not in VARIANT_PARAMS:
        raise ValueError(f"unknown variant {variant!r}; pre-registered: {sorted(VARIANT_PARAMS)}")
    vp = VARIANT_PARAMS[variant]
    if ep is not None and expected is None: expected, _ = entry_trend_mapping(ep)
    expected = expected or TrendDirection.UNKNOWN
    _warmup_from = _fmt(_dt(ts_from) - timedelta(minutes=60))
    five = agg_5m(keys, bars, "NEAR", _warmup_from, ts_to)
    telemetry = []
    _entry_idx = next((i for i, b in enumerate(five) if _dt(b["asof_ts"]) >= _dt(ts_from)), 0)
    for i in range(_entry_idx, len(five)):
        decision_bar = five[i]
        dts = decision_bar["ts"]
        f_before = five[:i + 1]
        if len(f_before) < 12:
            telemetry.append({"decision_ts": dts, "block_reason": "DIVERGENCE_OR_INSUFFICIENT", "signal_timestamps": {"adl": dts, "regime_60m": None, "exit_15m": None}})
            continue
        # Completed 60m and 15m blocks are formed strictly from bars ending at dts.
        h = _aggregate_completed_5m_blocks(f_before, 60)
        q = _aggregate_completed_5m_blocks(f_before, 15)
        if not h:
            telemetry.append({"decision_ts": dts, "block_reason": "INSUFFICIENT_SAME_DIRECTION", "signal_timestamps": {"adl": dts, "regime_60m": None, "exit_15m": None}})
            continue
        regime = _direction(h[-1])
        exit_dir = _direction(q[-1]) if q else TrendDirection.UNKNOWN
        if vp["chop_vetoes"]:
            # STRICT (spec CHOP->BLOCK matrix): ANY mismatch with expected
            # blocks, including a CHOP regime or CHOP exit.
            _regime_block = regime != expected
            _exit_block = exit_dir != expected
        else:
            # TOLERANT (P1 loose 2-of-3): CHOP regime/exit does NOT veto; only
            # a genuinely OPPOSITE (divergent) block does. Fail-closed on
            # UNKNOWN. The 2-of-3 arbitration inside the pipeline decides.
            _opposite_regime = regime in (TrendDirection.BEARISH, TrendDirection.BULLISH) and regime != expected
            _opposite_exit = exit_dir in (TrendDirection.BEARISH, TrendDirection.BULLISH) and exit_dir != expected
            _regime_block = regime == TrendDirection.UNKNOWN or _opposite_regime
            _exit_block = exit_dir == TrendDirection.UNKNOWN or _opposite_exit
        if _regime_block or _exit_block:
            reason = "INSUFFICIENT_SAME_DIRECTION"
            telemetry.append({"decision_ts": dts, "block_reason": reason, "signal_timestamps": {"adl": dts, "regime_60m": h[-1]["ts"], "exit_15m": q[-1]["ts"] if q else None}})
            continue
        velocity = None
        if vp["velocity_check"]:
            velocity = _spread_velocity_15m(f_before, far_keys, far_bars, expected, completed_15m=q)
            v_min = float(vp.get("velocity_min_abs_slope_pts") or 0.0)
            if velocity is None or abs(velocity["slope"]) < v_min:
                reason = "VELOCITY_FLAT"
                telemetry.append({"decision_ts": dts, "block_reason": reason, "signal_timestamps": {"adl": dts, "regime_60m": h[-1]["ts"], "exit_15m": q[-1]["ts"] if q else None, "velocity_15m": None}})
                continue
            if not velocity["aligned"]:
                reason = "VELOCITY_OPPOSITE"
                telemetry.append({"decision_ts": dts, "block_reason": reason, "signal_timestamps": {"adl": dts, "regime_60m": h[-1]["ts"], "exit_15m": q[-1]["ts"] if q else None, "velocity_15m": velocity}})
                continue
        d = _trend_decision(dts, f_before, expected)
        d["regime_60m"] = regime.value; d["exit_15m_direction"] = exit_dir.value
        d["signal_timestamps"] = {"adl": dts, "regime_60m": h[-1]["ts"], "exit_15m": q[-1]["ts"]}
        if velocity is not None:
            d["velocity_15m"] = {"slope": round(velocity["slope"], 6), "aligned": velocity["aligned"], "n": velocity["n"]}
        telemetry.append(d)
        if d.get("pass_release") and d.get("direction") == expected.value:  # P1: exact-direction pass only
            j = i + 1
            if j < len(five):
                d["bar_ts"] = dts; d["execution_ts"] = five[j]["ts"]; d["execution_bar"] = five[j]
                return d
            d["block_reason"] = "NO_NEXT_EXECUTION_BAR"
    first = telemetry[0] if telemetry else {
        "decision_ts": None, "block_reason": "INSUFFICIENT_SAME_DIRECTION",
        "signal_timestamps": {},
    }
    return {**first, "telemetry": telemetry}


def _entry_price(ep, leg): return float((ep["entry_near"] if leg == "NEAR" else ep["entry_far"]).get("price") or 0)
def _leg_side(ep, leg): return str((ep["entry_near"] if leg == "NEAR" else ep["entry_far"]).get("side") or "")
def _lifecycle_horizon(ep):
    x = ep["exit"] or ep["release"] or ep["entry_near"]
    return normalize_ts(x.get("timestamp", ""))


def _empty_stats():
    return {"eligible": 0, "skipped": 0, "pnl": 0.0, "pnls": [], "avg_pnl": 0.0, "max_drawdown": 0.0,
            "release": 0, "combined": 0, "exit_count": 0, "coverage": 0.0}


def _peak_trough_drawdown(pnls):
    cum = peak = 0.0; mdd = 0.0
    for p in pnls:
        cum += p; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    return mdd


def _simulate_arm(arm, episodes, keys, near, far, telemetry_out=None, variant="TOLERANT"):
    st = _empty_stats()
    for ep in episodes:
        an, af = ep["entry_near"], ep["entry_far"]
        if an is None or af is None or (ep["exit"] is None and ep["release"] is None): st["skipped"] += 1; continue
        entry_ts, horizon_ts = normalize_ts(an.get("timestamp", "")), _lifecycle_horizon(ep)
        if bar_at_or_before(keys, near, entry_ts) is None or bar_at_or_before(keys, near, horizon_ts) is None: st["skipped"] += 1; continue
        st["eligible"] += 1
        exit_px = bar_at_or_before(keys, near, horizon_ts)["close"]
        pnl = 0.0; released = combined = False
        if arm == "BASELINE_SINGLE_LEG_RELEASE" and ep["release"] is not None:
            released = True; leg = str(ep["release"].get("leg") or "NEAR"); px = float(ep["release"].get("price") or exit_px)
            pnl += leg_pnl(_entry_price(ep, leg), px, _leg_side(ep, leg)); other = "FAR" if leg == "NEAR" else "NEAR"; pnl += leg_pnl(_entry_price(ep, other), exit_px, _leg_side(ep, other))
        elif arm == "TREND_CONFIRMED_RELEASE":
            expected, rel_leg = entry_trend_mapping(ep)
            conf = walk_trend_confirmation(keys, near, entry_ts, horizon_ts, expected, ep,
                                           variant=variant, far_bars=far, far_keys=sorted(far))
            if telemetry_out is not None: telemetry_out[ep["trade_id"]] = conf
            if conf and conf.get("execution_bar") and rel_leg:
                released = True; px = conf["execution_bar"]["close"]; pnl += leg_pnl(_entry_price(ep, rel_leg), px, _leg_side(ep, rel_leg)); other = "FAR" if rel_leg == "NEAR" else "NEAR"; pnl += leg_pnl(_entry_price(ep, other), exit_px, _leg_side(ep, other))
            else:
                combined = True; pnl += leg_pnl(_entry_price(ep, "NEAR"), exit_px, _leg_side(ep, "NEAR")) + leg_pnl(_entry_price(ep, "FAR"), exit_px, _leg_side(ep, "FAR"))
        else:
            combined = True; pnl += leg_pnl(_entry_price(ep, "NEAR"), exit_px, _leg_side(ep, "NEAR")) + leg_pnl(_entry_price(ep, "FAR"), exit_px, _leg_side(ep, "FAR"))
        st["pnls"].append(pnl); st["pnl"] += pnl; st["exit_count"] += 2; st["release"] += int(released); st["combined"] += int(combined)
    n = len(st["pnls"]); st["avg_pnl"] = round(st["pnl"] / n, 2) if n else 0.0; st["max_drawdown"] = round(_peak_trough_drawdown(st["pnls"]), 2)
    den = st["eligible"] + st["skipped"]; st["coverage"] = round(st["eligible"] / den, 4) if den else 0.0
    return st


def gates_met(eligible, release, has_modules, block_distribution, error=None):
    failed = []
    if eligible < MIN_ELIGIBLE_FOR_APPROVAL: failed.append("eligible_below_minimum")
    if release <= 0: failed.append("no_trend_confirmed_release")
    if not has_modules: failed.append("modules_unavailable")
    if block_distribution and block_distribution.get("MODULES_UNAVAILABLE", 0) > max(0, sum(block_distribution.values()) / 2): failed.append("modules_unavailable_dominates")
    if error: failed.append("no_fills_log_error")
    return not failed, failed


def _block_distribution(telemetry: dict) -> dict:
    dist = {}
    for t in telemetry.values():
        reason = t.get("block_reason") if isinstance(t, dict) else "UNKNOWN"
        if reason: dist[reason] = dist.get(reason, 0) + 1
    return dist


def run_replay(fills_path="", bars_root="", variant="TOLERANT"):
    if variant not in VARIANT_PARAMS:
        raise ValueError(f"unknown variant {variant!r}; pre-registered: {sorted(VARIANT_PARAMS)}")
    root = bars_root or "."; fills_path = fills_path or discover_fills_log(root)
    if not fills_path: return {"error": "no fills log found", "verdict": "HOLD"}
    fills = load_fills(str(fills_path)); episodes = build_episodes(fills); near = load_bars(discover_bar_files(root, "near"), "near"); far = load_bars(discover_bar_files(root, "far"), "far"); keys = sorted(near)
    telemetry = {}; stats = {a: _simulate_arm(a, episodes, keys, near, far, telemetry if a == "TREND_CONFIRMED_RELEASE" else None, variant=variant) for a in ARMS}
    dist = _block_distribution(telemetry)
    eligible = stats[ARMS[0]]["eligible"]; release = stats["TREND_CONFIRMED_RELEASE"]["release"]; ok, failed = gates_met(eligible, release, HAS_MODULES, dist)
    status = "OBSERVED" if ok else ("TREND_UNTESTED_NO_CONFIRM" if release == 0 else "RESEARCH_INSUFFICIENT_SAMPLE")
    return {"fills_path": str(fills_path), "near_files": len(discover_bar_files(root, "near")), "far_files": len(discover_bar_files(root, "far")), "episodes": len(episodes), "eligible": eligible, "status": status, "verdict": "READY_FOR_APPROVAL" if ok else "HOLD", "coverage": max((s["coverage"] for s in stats.values()), default=0), "arms": stats, "block_reason_distribution": dist, "episode_first_confirm_or_block": telemetry, "gates": {"met": ok, "failed": failed}, "HAS_MODULES": HAS_MODULES, "variant": variant, "variant_params": _variant_params(variant), "shared_cost": SHARED_COST}


def run_all_variants(fills_path="", bars_root=""):
    """Phase 2: replay EVERY frozen variant under the shared cost model."""
    root = bars_root or "."
    fills_path = fills_path or discover_fills_log(root)
    if not fills_path: return {"error": "no fills log found", "verdict": "HOLD"}
    per_variant = {}
    for v in VALID_VARIANTS:
        res = run_replay(fills_path, root, variant=v)
        t = res["arms"]["TREND_CONFIRMED_RELEASE"]
        per_variant[v] = {
            "params": _variant_params(v),
            "eligible": res["eligible"], "coverage": res["coverage"],
            "status": res["status"], "verdict": res["verdict"], "gates": res["gates"],
            "arms": res["arms"], "block_reason_distribution": res["block_reason_distribution"],
            "trend_release": t["release"], "trend_combined": t["combined"],
            "trend_pnl": round(t["pnl"], 2), "trend_avg_pnl": t["avg_pnl"],
            "trend_max_drawdown": t["max_drawdown"], "trend_exit_count": t["exit_count"],
        }
    src = SELF_PATH.read_text(encoding="utf-8") if SELF_PATH.exists() else ""
    return {
        "harness": "mts_trend_replay_v1", "version": "1.2", "mode": "variants",
        "content_sha256": hashlib.sha256(src.encode()).hexdigest(),
        "fills_path": str(fills_path), "episodes": len(load_fills(str(fills_path))),
        "shared_cost": SHARED_COST, "eligible": per_variant["TOLERANT"]["eligible"],
        "variants": per_variant,
    }


# ---- Phase 3: multi-window walk-forward OOS --------------------------------
def _episode_entry_date(ep: dict) -> str:
    an = ep.get("entry_near")
    return normalize_ts(an.get("timestamp", ""))[:10] if an else ""


def split_windows(fills: list[dict], n_windows: int = 2,
                  n_oos_windows: int = 1) -> list[dict]:
    """Deterministic split of fills-covered dates into contiguous windows.

    The LAST ``n_oos_windows`` windows are designated out-of-sample (OOS);
    earlier windows serve only as observation.  Windows are disjoint and cover
    every fills date.  Parameters are pre-registered and fixed (no tuning on
    the eval window).
    """
    dates = sorted({normalize_ts(f.get("timestamp", ""))[:10] for f in fills if f.get("timestamp")})
    dates = [d for d in dates if d]
    if not dates:
        return []
    n = min(n_windows, len(dates))
    n_oos = max(1, min(n_oos_windows, n))
    groups = [dates[i * len(dates) // n:(i + 1) * len(dates) // n] for i in range(n)]
    return [{"name": f"W{i + 1}", "dates": g, "oos": i >= n - n_oos} for i, g in enumerate(groups)]


def run_walk_forward(fills_path="", bars_root="", variant="TOLERANT",
                     windows=None, n_windows=2) -> dict:
    """Phase 3: per-window replay + OOS verdict under a FIXED variant.

    Verdict rule: OOS variant is ROBUST iff (a) OOS pnl > OOS baseline pnl
    under the SAME pessimistic cost model, AND (b) OOS release count > 0,
    AND (c) coverage >= 0.9.  OOS with too few eligible episodes (or losing to
    baseline) -> HOLD with an explicit reason.  No parameter optimization on
    the evaluation window ever happens (no_eval_window_tuning=True).
    """
    if variant not in VARIANT_PARAMS:
        raise ValueError(f"unknown variant {variant!r}; pre-registered: {sorted(VARIANT_PARAMS)}")
    root = bars_root or "."
    fills_path = fills_path or discover_fills_log(root)
    if not fills_path: return {"error": "no fills log found", "verdict": "HOLD"}
    fills = load_fills(str(fills_path)); episodes = build_episodes(fills)
    near = load_bars(discover_bar_files(root, "near"), "near")
    far = load_bars(discover_bar_files(root, "far"), "far")
    keys = sorted(near)
    windows = windows or split_windows(fills, n_windows)
    per_window = []
    for w in windows:
        w_dates = set(w["dates"])
        w_eps = [ep for ep in episodes if _episode_entry_date(ep) in w_dates]
        telemetry = {}
        baseline = _simulate_arm("BASELINE_SINGLE_LEG_RELEASE", w_eps, keys, near, far)
        trend = _simulate_arm("TREND_CONFIRMED_RELEASE", w_eps, keys, near, far,
                              telemetry_out=telemetry, variant=variant)
        norevt = _simulate_arm("NO_REVT", w_eps, keys, near, far)
        per_window.append({
            "name": w["name"], "dates": list(w["dates"]), "oos": bool(w["oos"]),
            "episodes": len(w_eps),
            "eligible": trend["eligible"], "skipped": trend["skipped"], "coverage": trend["coverage"],
            "pnl": round(trend["pnl"], 2), "avg_pnl": trend["avg_pnl"], "max_drawdown": trend["max_drawdown"],
            "release": trend["release"], "combined": trend["combined"], "exit_count": trend["exit_count"],
            "baseline_pnl": round(baseline["pnl"], 2), "baseline_release": baseline["release"],
            "norevt_pnl": round(norevt["pnl"], 2),
            "block_reason_distribution": _block_distribution(telemetry),
        })
    oos_windows = [w for w in per_window if w["oos"]]
    if not per_window or not oos_windows:
        return {"harness": "mts_trend_replay_v1", "version": "1.3", "mode": "walk_forward",
                "content_sha256": _self_sha256(), "variant": variant,
                "variant_params": _variant_params(variant), "no_eval_window_tuning": True,
                "fills_path": str(fills_path), "episodes": len(episodes),
                "shared_cost": SHARED_COST, "windows": per_window,
                "oos": None, "status": "RESEARCH_INSUFFICIENT_OOS_SAMPLE",
                "verdict": "HOLD",
                "verdict_reason": "oos_insufficient_eligible:no_oos_windows"}
    oos = oos_windows[-1]  # display metrics of the LAST OOS window (unchanged)
    oos_eligible_total = sum(w["eligible"] for w in oos_windows)
    n_oos = len(oos_windows)
    oos_pnl_total = sum(w["pnl"] for w in oos_windows)
    oos_baseline_total = sum(w["baseline_pnl"] for w in oos_windows)
    oos_release_total = sum(w["release"] for w in oos_windows)
    oos_coverage_min = min(w["coverage"] for w in oos_windows)
    reasons = []
    if oos_eligible_total < OOS_MIN_ELIGIBLE:
        reasons.append(f"oos_insufficient_eligible:{oos_eligible_total}<{OOS_MIN_ELIGIBLE}")
    if n_oos < OOS_MIN_WINDOWS:
        reasons.append(f"oos_insufficient_windows:{n_oos}<{OOS_MIN_WINDOWS}")
    if not (oos_pnl_total > oos_baseline_total):
        reasons.append(f"oos_pnl_not_above_baseline:{oos_pnl_total}<={oos_baseline_total}")
    if oos_release_total <= 0:
        reasons.append("oos_no_release")
    if oos_coverage_min < 0.9:
        reasons.append(f"oos_coverage_below_0.9:{oos_coverage_min}")
    robust = not reasons
    if not robust and any(r.startswith("oos_insufficient") for r in reasons):
        status = "RESEARCH_INSUFFICIENT_OOS_SAMPLE"
    else:
        status = "OBSERVED" if robust else "RESEARCH_INSUFFICIENT_SAMPLE"
    return {
        "harness": "mts_trend_replay_v1", "version": "1.3", "mode": "walk_forward",
        "content_sha256": _self_sha256(), "variant": variant,
        "variant_params": _variant_params(variant), "no_eval_window_tuning": True,
        "fills_path": str(fills_path), "episodes": len(episodes),
        "shared_cost": SHARED_COST, "windows": per_window,
        "oos": {"name": oos["name"], "dates": list(oos["dates"]), "episodes": oos["episodes"],
                "eligible": oos["eligible"], "coverage": oos["coverage"],
                "pnl": oos["pnl"], "baseline_pnl": oos["baseline_pnl"],
                "avg_pnl": oos["avg_pnl"], "max_drawdown": oos["max_drawdown"],
                "release": oos["release"], "combined": oos["combined"], "exit_count": oos["exit_count"],
                "block_reason_distribution": oos["block_reason_distribution"]},
        "oos_summary": {
            "windows": n_oos,
            "eligible_total": oos_eligible_total,
            "pnl_total": round(oos_pnl_total, 2),
            "baseline_pnl_total": round(oos_baseline_total, 2),
            "release_total": oos_release_total,
            "coverage_min": round(oos_coverage_min, 4),
        },
        "status": status,
        "verdict": "ROBUST" if robust else "HOLD",
        "verdict_reason": "; ".join(reasons) if reasons else "all_oos_conditions_met",
    }


def _self_sha256() -> str:
    src = SELF_PATH.read_text(encoding="utf-8") if SELF_PATH.exists() else ""
    return hashlib.sha256(src.encode()).hexdigest()


def build_manifest(arms_stats, eligible, coverage, *, block_reason_distribution=None, episode_first_confirm_or_block=None, status=None, verdict=None, gates=None, variant=None, variants=None, walk_forward=None, **kwargs):
    src = SELF_PATH.read_text(encoding="utf-8") if SELF_PATH.exists() else ""
    m = {"harness": "mts_trend_replay_v1", "version": "1.2", "content_sha256": hashlib.sha256(src.encode()).hexdigest(), "eligible": eligible, "coverage": round(coverage, 4), "coverage_per_arm": {k: v["coverage"] for k, v in arms_stats.items()}, "arms": arms_stats, "block_reason_distribution": block_reason_distribution or {}, "episode_first_confirm_or_block": episode_first_confirm_or_block or {}, "status": status or ("OBSERVED" if verdict == "READY_FOR_APPROVAL" else "RESEARCH_INSUFFICIENT_SAMPLE"), "verdict": verdict or "HOLD", "gates": gates or {}, "shared_cost": SHARED_COST}
    if variant is not None:
        m["variant"] = variant
        m["variant_params"] = _variant_params(variant)
    if variants is not None:
        m["variants"] = variants
    if walk_forward is not None:
        m["walk_forward"] = walk_forward
    return m


def _print_window(w):
    print(f"[{w['name']}] oos={w['oos']} dates={w['dates']} episodes={w['episodes']}")
    print(f"  eligible={w['eligible']} skipped={w['skipped']} coverage={w['coverage']}")
    print(f"  pnl={w['pnl']}  avg_pnl={w['avg_pnl']}  max_drawdown={w['max_drawdown']}")
    print(f"  release={w['release']} combined={w['combined']} exit_count={w['exit_count']}  baseline_pnl={w['baseline_pnl']} baseline_release={w['baseline_release']} norevt_pnl={w['norevt_pnl']}")
    print("  block_reason_distribution:", json.dumps(w["block_reason_distribution"], sort_keys=True))


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv[1:]
    mode, variant, n_windows, pos = "single", "TOLERANT", 2, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--all-variants": mode = "all"
        elif a == "--walk-forward": mode = "walk_forward"
        elif a == "--variant":
            i += 1
            if i < len(argv): variant = argv[i]
        elif a == "--windows":
            i += 1
            if i < len(argv):
                try: n_windows = int(argv[i])
                except ValueError: n_windows = 2
        else: pos.append(a)
        i += 1
    root = pos[0] if pos else "."
    fills = pos[1] if len(pos) > 1 else ""
    out = Path(root) / "scripts/research/output"; out.mkdir(parents=True, exist_ok=True)
    if mode == "all":
        res = run_all_variants(fills, root)
        if res.get("error"): print("mts_trend_replay_v1:", res["error"], "under", root); return 2
        path = out / "mts_trend_replay_v1_manifest_variants.json"
        path.write_text(json.dumps(res, indent=2))
        print("=" * 76); print("MTS 2.0 TREND-RELEASE FROZEN VARIANTS (STRICT / TOLERANT / TOLERANT_VELOCITY)"); print("=" * 76)
        print(f"fills_log   : {res['fills_path']}\nepisodes    : {res['episodes']}\ncontent_sha256: {res['content_sha256']}\nshared_cost : {json.dumps(res['shared_cost'])}")
        print("-" * 76)
        for v, d in res["variants"].items():
            print(f"[{v}] params={json.dumps(d['params'], sort_keys=True)}")
            print(f"  eligible={d['eligible']} coverage={d['coverage']} release={d['trend_release']} combined={d['trend_combined']} pnl={d['trend_pnl']} avg_pnl={d['trend_avg_pnl']} max_drawdown={d['trend_max_drawdown']} exit_count={d['trend_exit_count']} status={d['status']} verdict={d['verdict']}")
            print("  block_reason_distribution:", json.dumps(d["block_reason_distribution"], sort_keys=True))
        print("=" * 76); print("manifest ->", path); return 0
    if mode == "walk_forward":
        res = run_walk_forward(fills, root, variant=variant, n_windows=n_windows)
        if res.get("error"): print("mts_trend_replay_v1:", res["error"], "under", root); return 2
        path = out / "mts_trend_replay_v1_manifest_walkforward.json"
        path.write_text(json.dumps(res, indent=2))
        print("=" * 76); print(f"MTS 2.0 WALK-FORWARD OOS (variant={res['variant']})"); print("=" * 76)
        print(f"fills_log   : {res['fills_path']}\nepisodes    : {res['episodes']}\ncontent_sha256: {res['content_sha256']}\nno_eval_window_tuning: {res['no_eval_window_tuning']}\nvariant_params: {json.dumps(res['variant_params'], sort_keys=True)}\nshared_cost : {json.dumps(res['shared_cost'])}")
        print("-" * 76)
        for w in res["windows"]: _print_window(w)
        print("-" * 76)
        oos = res.get("oos")
        if oos:
            print(f"[OOS {oos['name']}] eligible={oos['eligible']} coverage={oos['coverage']} pnl={oos['pnl']} baseline_pnl={oos['baseline_pnl']} release={oos['release']} combined={oos['combined']} exit_count={oos['exit_count']} avg_pnl={oos['avg_pnl']} max_drawdown={oos['max_drawdown']}")
            print("  block_reason_distribution:", json.dumps(oos["block_reason_distribution"], sort_keys=True))
        if res.get("oos_summary"):
            s = res["oos_summary"]
            print(f"OOS summary : windows={s['windows']} eligible_total={s['eligible_total']} pnl_total={s['pnl_total']} baseline_pnl_total={s['baseline_pnl_total']} release_total={s['release_total']} coverage_min={s['coverage_min']}")
        print(f"OOS status  : {res.get('status')}\nOOS verdict : {res['verdict']}\nreason      : {res['verdict_reason']}")
        print("=" * 76); print("manifest ->", path); return 0
    res = run_replay(fills, root, variant=variant)
    if res.get("error"): print("mts_trend_replay_v1: no fills log found under", root); return 2
    manifest = build_manifest(res["arms"], res["eligible"], res["coverage"], block_reason_distribution=res["block_reason_distribution"], episode_first_confirm_or_block=res["episode_first_confirm_or_block"], status=res["status"], verdict=res["verdict"], gates=res["gates"], variant=res["variant"])
    print("=" * 76); print("MTS 2.0 TREND-RELEASE COUNTERFACTUAL REPLAY (mts_trend_replay_v1)"); print("=" * 76)
    print(f"fills_log   : {res['fills_path']}\nepisodes    : {res['episodes']}\nnear_files  : {res['near_files']}\nfar_files   : {res['far_files']}\neligibility : {res['eligible']} (min required {MIN_ELIGIBLE_FOR_APPROVAL})\nstatus      : {res['status']}\nverdict     : {res['verdict']}\nvariant     : {res['variant']}\nvariant_params: {json.dumps(res['variant_params'], sort_keys=True)}\nshared_cost : {json.dumps(res['shared_cost'])}")
    print("-" * 76)
    for arm, s in res["arms"].items(): print(f"[{arm}]\n  eligible={s['eligible']} skipped={s['skipped']} coverage={s['coverage']}\n  pnl={round(s['pnl'],2)}  avg_pnl={s['avg_pnl']}  max_drawdown={s['max_drawdown']}\n  release={s['release']} combined={s['combined']} exit_count={s['exit_count']}")
    print("block_reason_distribution:", json.dumps(res["block_reason_distribution"], sort_keys=True)); print("=" * 76)
    path = out / "mts_trend_replay_v1_manifest.json"; path.write_text(json.dumps(manifest, indent=2)); print("manifest ->", path); return 0

if __name__ == "__main__": raise SystemExit(main())

__all__ = ["agg_5m", "entry_trend_mapping", "walk_trend_confirmation", "_trend_decision", "gates_met", "run_replay", "run_all_variants", "run_walk_forward", "split_windows", "build_manifest", "cost", "leg_pnl", "VARIANT_PARAMS", "VALID_VARIANTS", "ARMS", "MIN_ELIGIBLE_FOR_APPROVAL", "OOS_MIN_ELIGIBLE", "OOS_MIN_WINDOWS", "SHARED_COST"]
