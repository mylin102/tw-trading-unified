#!/usr/bin/env python3
"""As-of-only MTS 2.0 trend-release counterfactual replay.

Input files contain irregular 1-minute OHLCV observations.  All decisions are
made from completed 5-minute bars; the 60-minute regime and 15-minute
confirmation are also completed-block signals.  A confirmed signal executes
on the next available completed 5-minute bar, never on the signal bar.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

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

POINT_VALUE = 10.0
BROKER_FEE = 20.0
TAX_RATE = 2e-5
MIN_ELIGIBLE_FOR_APPROVAL = 30
ARMS = ("BASELINE_SINGLE_LEG_RELEASE", "TREND_CONFIRMED_RELEASE", "NO_REVT")
SELF_PATH = Path(__file__)


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


def walk_trend_confirmation(keys: list[str], bars: dict[str, dict],
                            ts_from: str, ts_to: str, expected: TrendDirection | None = None,
                            ep: dict | None = None) -> dict | None:
    """Find earliest as-of decision where completed 60m, 15m and pipeline align."""
    if ep is not None and expected is None: expected, _ = entry_trend_mapping(ep)
    expected = expected or TrendDirection.UNKNOWN
    five = agg_5m(keys, bars, "NEAR", ts_from, ts_to)
    telemetry = []
    for i, decision_bar in enumerate(five):
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
        if regime != expected or exit_dir != expected:
            reason = "INSUFFICIENT_SAME_DIRECTION"
            telemetry.append({"decision_ts": dts, "block_reason": reason, "signal_timestamps": {"adl": dts, "regime_60m": h[-1]["ts"], "exit_15m": q[-1]["ts"] if q else None}})
            continue
        d = _trend_decision(dts, f_before, expected)
        d["regime_60m"] = regime.value; d["exit_15m_direction"] = exit_dir.value
        d["signal_timestamps"] = {"adl": dts, "regime_60m": h[-1]["ts"], "exit_15m": q[-1]["ts"]}
        telemetry.append(d)
        if d.get("pass_release") and d.get("direction") == expected.value:
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


def _simulate_arm(arm, episodes, keys, near, far, telemetry_out=None):
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
            conf = walk_trend_confirmation(keys, near, entry_ts, horizon_ts, expected, ep)
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


def run_replay(fills_path="", bars_root=""):
    root = bars_root or "."; fills_path = fills_path or discover_fills_log(root)
    if not fills_path: return {"error": "no fills log found", "verdict": "HOLD"}
    fills = load_fills(str(fills_path)); episodes = build_episodes(fills); near = load_bars(discover_bar_files(root, "near"), "near"); far = load_bars(discover_bar_files(root, "far"), "far"); keys = sorted(near)
    telemetry = {}; stats = {a: _simulate_arm(a, episodes, keys, near, far, telemetry if a == "TREND_CONFIRMED_RELEASE" else None) for a in ARMS}
    dist = {}
    for t in telemetry.values():
        reason = t.get("block_reason") if isinstance(t, dict) else "UNKNOWN"
        if reason: dist[reason] = dist.get(reason, 0) + 1
    eligible = stats[ARMS[0]]["eligible"]; release = stats["TREND_CONFIRMED_RELEASE"]["release"]; ok, failed = gates_met(eligible, release, HAS_MODULES, dist)
    status = "OBSERVED" if ok else ("TREND_UNTESTED_NO_CONFIRM" if release == 0 else "RESEARCH_INSUFFICIENT_SAMPLE")
    return {"fills_path": str(fills_path), "near_files": len(discover_bar_files(root, "near")), "far_files": len(discover_bar_files(root, "far")), "episodes": len(episodes), "eligible": eligible, "status": status, "verdict": "READY_FOR_APPROVAL" if ok else "HOLD", "coverage": max((s["coverage"] for s in stats.values()), default=0), "arms": stats, "block_reason_distribution": dist, "episode_first_confirm_or_block": telemetry, "gates": {"met": ok, "failed": failed}, "HAS_MODULES": HAS_MODULES}


def build_manifest(arms_stats, eligible, coverage, *, block_reason_distribution=None, episode_first_confirm_or_block=None, status=None, verdict=None, gates=None, **kwargs):
    src = SELF_PATH.read_text(encoding="utf-8") if SELF_PATH.exists() else ""
    return {"harness": "mts_trend_replay_v1", "version": "1.1", "content_sha256": hashlib.sha256(src.encode()).hexdigest(), "eligible": eligible, "coverage": round(coverage, 4), "coverage_per_arm": {k: v["coverage"] for k, v in arms_stats.items()}, "arms": arms_stats, "block_reason_distribution": block_reason_distribution or {}, "episode_first_confirm_or_block": episode_first_confirm_or_block or {}, "status": status or ("OBSERVED" if verdict == "READY_FOR_APPROVAL" else "RESEARCH_INSUFFICIENT_SAMPLE"), "verdict": verdict or "HOLD", "gates": gates or {}}


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv[1:]; root = argv[0] if argv else "."; res = run_replay(argv[1] if len(argv) > 1 else "", root)
    if res.get("error"): print("mts_trend_replay_v1: no fills log found under", root); return 2
    manifest = build_manifest(res["arms"], res["eligible"], res["coverage"], block_reason_distribution=res["block_reason_distribution"], episode_first_confirm_or_block=res["episode_first_confirm_or_block"], status=res["status"], verdict=res["verdict"], gates=res["gates"])
    print("=" * 76); print("MTS 2.0 TREND-RELEASE COUNTERFACTUAL REPLAY (mts_trend_replay_v1)"); print("=" * 76)
    print(f"fills_log   : {res['fills_path']}\nepisodes    : {res['episodes']}\nnear_files  : {res['near_files']}\nfar_files   : {res['far_files']}\neligibility : {res['eligible']} (min required {MIN_ELIGIBLE_FOR_APPROVAL})\nstatus      : {res['status']}\nverdict     : {res['verdict']}")
    print("-" * 76)
    for arm, s in res["arms"].items(): print(f"[{arm}]\n  eligible={s['eligible']} skipped={s['skipped']} coverage={s['coverage']}\n  pnl={round(s['pnl'],2)}  avg_pnl={s['avg_pnl']}  max_drawdown={s['max_drawdown']}\n  release={s['release']} combined={s['combined']} exit_count={s['exit_count']}")
    print("block_reason_distribution:", json.dumps(res["block_reason_distribution"], sort_keys=True)); print("=" * 76)
    out = Path(root) / "scripts/research/output"; out.mkdir(parents=True, exist_ok=True); path = out / "mts_trend_replay_v1_manifest.json"; path.write_text(json.dumps(manifest, indent=2)); print("manifest ->", path); return 0

if __name__ == "__main__": raise SystemExit(main())

__all__ = ["agg_5m", "entry_trend_mapping", "walk_trend_confirmation", "_trend_decision", "gates_met", "run_replay", "build_manifest", "cost", "leg_pnl", "ARMS", "MIN_ELIGIBLE_FOR_APPROVAL"]
